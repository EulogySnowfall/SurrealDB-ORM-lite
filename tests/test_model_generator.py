"""Schema introspection (v0.30.0): the private model generator behind ``ModelCodeGenerator`` (spec §6.4).

The output boundaries (spec §6.4.1, §8; AC-12 boundary part) — the only ways server text reaches the generated source:
``_py_str()`` (a Python string literal that ``ast.literal_eval`` reads back exactly, with ruff format's quote) and
``_comment()`` (one printable-ASCII comment line, no trailing whitespace, cut to 128 columns), on the §8 corpus and on
2,000 seeded random strings. The names (spec §6.4.2; M37, M41; AC-17 part): which tables can be modelled (S1, S6),
the class-name pass (tables, then each table's sub-model candidates) and the field-name pass (reserved sets, every
class name of the module, aliases, collisions), deterministic from one run to the next.

``ModelCodeGenerator`` (spec §5.2, §6.4.3–§6.4.8; AC-9 unit, AC-11, AC-12, AC-17, AC-18 unit): the §9.3 golden from
the fixture as the parser reads it from the SurrealDB 3.x and 2.x renderings, every row of the kind → annotation table,
the field lines (``Computed`` and its server-form fallback, literal defaults, ``server_fields`` by attribute name,
aliases, ``READONLY``), sub-models, class blocks, the three ``model_config`` forms with their 128-column boundaries,
the module layout (imports, skip lines S1–S6), and generated modules that compile, pass ruff (isolated, the options
of spec §9.1) and import — with the injection corpus at every position. Unit tests only (no server).
"""

import ast
import builtins
import dataclasses
import importlib.util
import inspect
import io
import keyword
import random
import subprocess
import sys
import tokenize
import uuid
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import surreal_orm_lite
from surreal_orm_lite import Duration, ModelCodeGenerator
from surreal_orm_lite import introspection as introspection_module
from surreal_orm_lite import model_generator as model_generator_module
from surreal_orm_lite.define_parser import (
    parse_define_event,
    parse_define_field,
    parse_define_index,
    parse_define_table,
)
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
    _width,
)
from surreal_orm_lite.schema_state import (
    EventState,
    FieldState,
    IndexState,
    SchemaIssue,
    SchemaState,
    TableState,
)
from surreal_orm_lite.utils import _KEEP_LIMIT

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


#: A table's permissions when it defines none (``PERMISSIONS NONE``), as the reader gives them.
_TABLE_PERMISSIONS_NONE = dict.fromkeys(("select", "create", "update", "delete"), "NONE")


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


# === ModelCodeGenerator (spec §5.2, §6.4.3–§6.4.8) ====================================================================

#: The generated module's docstring (spec §6.4.8), exactly.
DOCSTRING = '''\
"""Models generated by surreal_orm_lite.generate_models_from_db() from a SurrealDB schema.

Review them before use. They describe tables that already exist: do not call
define_computed_fields() on them, it would replace the database's field definitions.
"""'''

#: Spec §9.3: the source generated for the portable fixture, read through the named connection "isp" — exactly.
GOLDEN = '''\
"""Models generated by surreal_orm_lite.generate_models_from_db() from a SurrealDB schema.

Review them before use. They describe tables that already exist: do not call
define_computed_fields() on them, it would replace the database's field definitions.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field
from surreal_orm_lite import (
    BaseSurrealEdge,
    BaseSurrealModel,
    Computed,
    Duration,
    PointField,
    SurrealConfigDict,
    computed,
)
from surrealdb import RecordID

# Not generated:
# - table 'isp-weird': its name is not an identifier, which table_name requires
# - table 'select': its name is a SurrealQL keyword the ORM cannot use as a table name


class IspCompany(BaseSurrealModel):
    # Comment: Companies
    model_config = SurrealConfigDict(table_name="isp_company", connection="isp")

    id: str | None = None
    name: str


class IspLikes(BaseSurrealEdge):
    # IN isp_person OUT isp_person | isp_company
    model_config = SurrealConfigDict(table_name="isp_likes", connection="isp")

    since: datetime | None = None


class IspLog(BaseSurrealModel):
    # SCHEMALESS: fields not declared here are kept (extra="allow")
    model_config = SurrealConfigDict(table_name="isp_log", connection="isp", extra="allow")

    id: str | None = None


class IspPersonAddress(BaseModel):
    city: str


class IspPerson(BaseSurrealModel):
    # Indexes: isp_person_email (UNIQUE)
    # Events: isp_person_created
    model_config = SurrealConfigDict(table_name="isp_person", connection="isp", server_fields=["created"])

    id: str | None = None
    address: IspPersonAddress | None = None
    age: int | None = 18
    balance: Decimal | None = None
    class_: str | None = Field(default=None, alias="class")
    created: datetime | None = None  # READONLY
    email: str
    employer: RecordID | None = None
    first_name: str | None = Field(default=None, alias="first-name")
    location: PointField = None
    meta: dict[str, Any] | None = None
    name: str
    status: Literal["active", "inactive"] = "active"
    tags: list[str]
    token: UUID | None = None
    trial: Duration | None = None
    updated: Computed[datetime] = computed("time::now()")
'''

#: The fixture's tables, events and indexes as SurrealDB renders them (measured on 3.3.4 and 2.7.0; the embedded
#: engine renders like 2.7.0). Table → (DEFINE TABLE, DEFINE FIELDs, DEFINE INDEXes, DEFINE EVENTs).
_FIXTURE_TABLES: dict[str, str] = {
    "isp-weird": "DEFINE TABLE `isp-weird` TYPE ANY SCHEMALESS PERMISSIONS NONE",
    "isp_company": "DEFINE TABLE isp_company TYPE NORMAL SCHEMAFULL COMMENT 'Companies' PERMISSIONS NONE",
    "isp_likes": "DEFINE TABLE isp_likes TYPE RELATION IN isp_person OUT isp_person | isp_company SCHEMAFULL PERMISSIONS NONE",
    "isp_log": "DEFINE TABLE isp_log TYPE ANY SCHEMALESS PERMISSIONS NONE",
    "isp_person": "DEFINE TABLE isp_person TYPE NORMAL SCHEMAFULL PERMISSIONS NONE",
    "select": "DEFINE TABLE `select` TYPE ANY SCHEMALESS PERMISSIONS NONE",
}
_FIXTURE_INDEXES = {"isp_person": ["DEFINE INDEX isp_person_email ON isp_person FIELDS email UNIQUE"]}
_FIXTURE_EVENTS = {
    "isp_person": [
        "DEFINE EVENT isp_person_created ON isp_person WHEN $event = 'CREATE' THEN (CREATE isp_log SET msg = 'created')"
    ]
}
_FIXTURE_FIELDS_3X = {
    "isp_company": ["DEFINE FIELD name ON isp_company TYPE string PERMISSIONS FULL"],
    "isp_likes": [
        "DEFINE FIELD in ON isp_likes TYPE record<isp_person> PERMISSIONS FULL",
        "DEFINE FIELD out ON isp_likes TYPE record<isp_person | isp_company> PERMISSIONS FULL",
        "DEFINE FIELD since ON isp_likes TYPE none | datetime PERMISSIONS FULL",
    ],
    "isp_person": [
        "DEFINE FIELD `first-name` ON isp_person TYPE none | string PERMISSIONS FULL",
        "DEFINE FIELD address ON isp_person TYPE none | object PERMISSIONS FULL",
        "DEFINE FIELD address.city ON isp_person TYPE string PERMISSIONS FULL",
        "DEFINE FIELD age ON isp_person TYPE none | int DEFAULT 18 PERMISSIONS FULL",
        "DEFINE FIELD balance ON isp_person TYPE none | decimal PERMISSIONS FULL",
        "DEFINE FIELD class ON isp_person TYPE none | string PERMISSIONS FULL",
        "DEFINE FIELD created ON isp_person TYPE datetime DEFAULT time::now() READONLY PERMISSIONS FULL",
        "DEFINE FIELD email ON isp_person TYPE string VALUE string::lowercase($value) PERMISSIONS FULL",
        "DEFINE FIELD employer ON isp_person TYPE none | record<isp_company> PERMISSIONS FULL",
        "DEFINE FIELD location ON isp_person TYPE none | geometry<point> PERMISSIONS FULL",
        "DEFINE FIELD meta ON isp_person TYPE none | object FLEXIBLE PERMISSIONS FULL",
        "DEFINE FIELD name ON isp_person TYPE string ASSERT string::len($value) > 0 PERMISSIONS FULL",
        "DEFINE FIELD status ON isp_person TYPE 'active' | 'inactive' DEFAULT 'active' PERMISSIONS FULL",
        "DEFINE FIELD tags ON isp_person TYPE array<string> PERMISSIONS FULL",
        "DEFINE FIELD tags.* ON isp_person TYPE string PERMISSIONS FULL",
        "DEFINE FIELD token ON isp_person TYPE none | uuid PERMISSIONS FULL",
        "DEFINE FIELD trial ON isp_person TYPE none | duration PERMISSIONS FULL",
        "DEFINE FIELD updated ON isp_person TYPE datetime VALUE time::now() PERMISSIONS FULL",
    ],
}
_FIXTURE_FIELDS_2X = {
    "isp_company": ["DEFINE FIELD name ON isp_company TYPE string PERMISSIONS FULL"],
    "isp_likes": [
        "DEFINE FIELD in ON isp_likes TYPE record<isp_person> PERMISSIONS FULL",
        "DEFINE FIELD out ON isp_likes TYPE record<isp_person | isp_company> PERMISSIONS FULL",
        "DEFINE FIELD since ON isp_likes TYPE option<datetime> PERMISSIONS FULL",
    ],
    "isp_person": [
        "DEFINE FIELD `first-name` ON isp_person TYPE option<string> PERMISSIONS FULL",
        "DEFINE FIELD address ON isp_person TYPE option<object> PERMISSIONS FULL",
        "DEFINE FIELD address.city ON isp_person TYPE string PERMISSIONS FULL",
        "DEFINE FIELD age ON isp_person TYPE option<int> DEFAULT 18 PERMISSIONS FULL",
        "DEFINE FIELD balance ON isp_person TYPE option<decimal> PERMISSIONS FULL",
        "DEFINE FIELD class ON isp_person TYPE option<string> PERMISSIONS FULL",
        "DEFINE FIELD created ON isp_person TYPE datetime DEFAULT time::now() READONLY PERMISSIONS FULL",
        "DEFINE FIELD email ON isp_person TYPE string VALUE string::lowercase($value) PERMISSIONS FULL",
        "DEFINE FIELD employer ON isp_person TYPE option<record<isp_company>> PERMISSIONS FULL",
        "DEFINE FIELD location ON isp_person TYPE option<geometry<point>> PERMISSIONS FULL",
        "DEFINE FIELD meta ON isp_person FLEXIBLE TYPE option<object> PERMISSIONS FULL",
        "DEFINE FIELD name ON isp_person TYPE string ASSERT string::len($value) > 0 PERMISSIONS FULL",
        "DEFINE FIELD status ON isp_person TYPE 'active' | 'inactive' DEFAULT 'active' PERMISSIONS FULL",
        "DEFINE FIELD tags ON isp_person TYPE array<string> PERMISSIONS FULL",
        "DEFINE FIELD tags[*] ON isp_person TYPE string PERMISSIONS FULL",
        "DEFINE FIELD token ON isp_person TYPE option<uuid> PERMISSIONS FULL",
        "DEFINE FIELD trial ON isp_person TYPE option<duration> PERMISSIONS FULL",
        "DEFINE FIELD updated ON isp_person TYPE datetime VALUE time::now() PERMISSIONS FULL",
    ],
}

#: The update() note of spec §6.4.7, exactly (fixed text, 124 columns at indent 4), narrowed by #261 to the datetimes
#: update() may not keep: below the top level, or past the first _KEEP_LIMIT top-level ones.
UPDATE_NOTE = "    # update() may fail on READONLY datetimes below the top level or past the 32nd (Python keeps microseconds): use merge()."

#: ruff's options for a generated module (spec §9.1).
_RUFF_CHECK = ["check", "--isolated", "--select", "E,F,W,I,UP,B,C4,SIM", "--target-version", "py311", "--line-length", "128"]
_RUFF_FORMAT = ["format", "--isolated", "--line-length", "128", "--check"]


def _parsed_table(
    table: str, fields: Sequence[str] = (), indexes: Sequence[str] = (), events: Sequence[str] = ()
) -> TableState:
    """A table as the reader builds it: the parsed ``DEFINE TABLE`` with its parsed fields, indexes and events."""
    parsed = [parse_define_field(statement).state for statement in fields]
    return dataclasses.replace(
        parse_define_table(table),
        fields={field.name: field for field in parsed},
        indexes={index.name: index for index in map(parse_define_index, indexes)},
        events={event.name: event for event in map(parse_define_event, events)},
    )


def _fixture_state(line: int, connection: str = "isp") -> SchemaState:
    """The §9.3 fixture as the parser reads it from SurrealDB ``line``'s renderings."""
    fields = _FIXTURE_FIELDS_3X if line == 3 else _FIXTURE_FIELDS_2X
    tables = {
        name: _parsed_table(statement, fields.get(name, ()), _FIXTURE_INDEXES.get(name, ()), _FIXTURE_EVENTS.get(name, ()))
        for name, statement in _FIXTURE_TABLES.items()
    }
    return SchemaState(tables=tables, connection=connection)


def _generate(*fields: FieldState, name: str = "t", connection: str = "default", **table: Any) -> str:
    """The module generated for one table ``name`` holding ``fields`` (built by keyword)."""
    state = TableState(name=name, fields={field.name: field for field in fields}, **table)
    return ModelCodeGenerator().generate(SchemaState(tables={name: state}, connection=connection))


def _f(name: str, kind: str = "string", **kwargs: Any) -> FieldState:
    """A field built by keyword; not nullable unless ``nullable=True`` is given."""
    kwargs.setdefault("nullable", False)
    return FieldState(name=name, field_type=kind, **kwargs)


#: #261 review: a ``READONLY`` datetime in a sub-object, after a timestamped array.
_META_AT = [_f("meta", "object"), _f("meta.at", "datetime", readonly=True)]


def _line_of(source: str, attribute: str) -> str:
    """The one line of ``source`` declaring ``attribute`` (stripped)."""
    found = [line.strip() for line in source.splitlines() if line.startswith(f"    {attribute}:")]
    assert len(found) == 1, (attribute, found, source)
    return found[0]


def _class_block(source: str, name: str) -> str:
    """The lines of class ``name`` in ``source``, up to the next top-level line."""
    lines = source.splitlines()
    start = lines.index(next(line for line in lines if line.startswith(f"class {name}(")))
    block = [lines[start]]
    for line in lines[start + 1 :]:
        if line and not line.startswith(" "):
            break
        block.append(line)
    return "\n".join(block).rstrip("\n") + "\n"


def _model_config(source: str, class_name: str | None = None) -> str:
    """The ``model_config`` text of class ``class_name`` — of the module's only class when ``None`` — (one line, or the
    wrapped form up to its ``    )``)."""
    lines = (source if class_name is None else _class_block(source, class_name)).splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("    model_config = "))
    if lines[start].endswith("("):
        end = lines.index("    )", start)
        return "\n".join(lines[start : end + 1])
    return lines[start]


def _ruff(tmp_path: Path, source: str, name: str = "generated") -> Path:
    """Write ``source`` to ``tmp_path`` and assert ruff check and ruff format --check (spec §9.1) accept it.

    The file is named ``gen_<name>.py``: ``python -m ruff`` runs with ``tmp_path`` as its working directory, where a
    file named like a standard module (``keyword.py``) would shadow it.
    """
    path = tmp_path / f"gen_{name}.py"
    path.write_text(source, encoding="utf-8")
    for arguments in (_RUFF_CHECK, _RUFF_FORMAT):
        result = subprocess.run(
            [sys.executable, "-m", "ruff", *arguments, str(path)],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            check=False,
        )
        assert result.returncode == 0, f"ruff {arguments[0]}:\n{result.stdout}{result.stderr}\n{source}"
    return path


def _import(tmp_path: Path, source: str) -> ModuleType:
    """Import ``source`` as a module written to ``tmp_path`` (removed from ``sys.modules`` afterwards)."""
    name = f"generated_{uuid.uuid4().hex}"
    path = tmp_path / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        del sys.modules[name]
    return module


def _comment_lines_fit(source: str) -> None:
    """Every comment of ``source`` ends by column 128; no line has trailing whitespace (AC-11)."""
    for line in source.splitlines():
        assert line == line.rstrip(), repr(line)
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            assert token.start[1] + len(token.string) <= 128, token.string


def _quality(tmp_path: Path, state: SchemaState, name: str) -> str:
    """AC-11: the module compiles, passes ruff, fits its comments, imports, and is the same string twice."""
    source = ModelCodeGenerator().generate(state)
    assert source == ModelCodeGenerator().generate(state)
    compile(source, f"{name}.py", "exec")
    _ruff(tmp_path, source, name)
    _comment_lines_fit(source)
    _import(tmp_path, source)
    return source


class TestGolden:
    """Spec §9.3 (AC-9 unit part): the fixture's source is the golden text, identical for both renderings."""

    def test_fixture_3x(self) -> None:
        assert ModelCodeGenerator().generate(_fixture_state(3)) == GOLDEN

    def test_fixture_2x(self) -> None:
        assert ModelCodeGenerator().generate(_fixture_state(2)) == GOLDEN

    def test_default_connection_has_no_connection_key(self) -> None:
        source = ModelCodeGenerator().generate(_fixture_state(3, connection="default"))
        assert source == GOLDEN.replace(', connection="isp"', "")
        assert "connection=" not in source

    def test_golden_passes_ruff_and_imports(self, tmp_path: Path) -> None:
        source = _quality(tmp_path, _fixture_state(3), "golden")
        assert source == GOLDEN
        module = _import(tmp_path, source)
        person = module.IspPerson
        assert person.get_table_name() == "isp_person"
        assert person.get_server_fields() == frozenset({"created", "updated"})
        assert person.get_computed_fields() == {"updated": "time::now()"}
        assert person.model_fields["class_"].alias == "class"
        assert module.IspLog.model_config["extra"] == "allow"
        assert issubclass(module.IspLikes, surreal_orm_lite.BaseSurrealEdge)


#: Spec §6.4.4: one field per row — (kind, nullable, the generated line).
TYPE_ROWS: list[tuple[str, bool, str]] = [
    ("any", False, "x: Any = None"),
    ("bool", False, "x: bool"),
    ("int", False, "x: int"),
    ("float", False, "x: float"),
    ("string", False, "x: str"),
    ("int", True, "x: int | None = None"),
    ("number", False, "x: int | float | Decimal"),
    ("decimal", False, "x: Decimal"),
    ("datetime", False, "x: datetime"),
    ("duration", False, "x: Duration"),
    ("uuid", False, "x: UUID"),
    ("bytes", False, "x: bytes"),
    ("range", False, "x: Range"),
    ("record", False, "x: RecordID"),
    ("record<a | b>", False, "x: RecordID"),
    ("record<a>", True, "x: RecordID | None = None"),
    ("geometry<point>", False, "x: GeometryPoint"),
    ("geometry<point>", True, "x: PointField = None"),
    ("geometry<line>", True, "x: LineStringField = None"),
    ("geometry<polygon>", True, "x: PolygonField = None"),
    ("geometry<multipoint>", True, "x: MultiPointField = None"),
    ("geometry<multiline>", True, "x: MultiLineStringField = None"),
    ("geometry<multipolygon>", True, "x: MultiPolygonField = None"),
    ("geometry<collection>", True, "x: GeometryCollectionField = None"),
    ("geometry<collection>", False, "x: GeometryCollection"),
    ("geometry", False, "x: Geometry"),
    ("geometry", True, "x: GeometryField = None"),
    ("geometry<feature>", False, "x: Geometry"),
    ("geometry<feature>", True, "x: GeometryField = None"),
    ("geometry<point | polygon>", False, "x: GeometryPoint | GeometryPolygon"),
    ("geometry<point | polygon>", True, "x: GeometryPoint | GeometryPolygon | None = None"),
    ("array<geometry<point>>", False, "x: list[GeometryPoint]"),
    ("geometry<point> | string", False, "x: GeometryPoint | str"),
    ("object", False, "x: dict[str, Any]"),
    ("object", True, "x: dict[str, Any] | None = None"),
    ("{ a: int }", False, "x: dict[str, Any]"),
    ("array", False, "x: list[Any]"),
    ("array<string>", False, "x: list[str]"),
    ("array<string, 10>", False, "x: list[str]"),
    ("array<option<int>, 5>", False, "x: list[int | None]"),
    ("array<object>", False, "x: list[dict[str, Any]]"),
    ("set", False, "x: list[Any]"),
    ("set<string>", False, "x: set[str]"),
    ("set<int, 3>", False, "x: set[int]"),
    ("set<float>", False, "x: set[float]"),
    ("set<bool>", False, "x: set[bool]"),
    ("set<bytes>", False, "x: set[bytes]"),
    ("set<decimal>", False, "x: set[Decimal]"),
    ("set<uuid>", False, "x: set[UUID]"),
    ("set<datetime>", False, "x: set[datetime]"),
    ("set<duration>", False, "x: set[Duration]"),
    ("set<'a' | 'b'>", False, 'x: set[Literal["a", "b"]]'),
    ("set<record<a>>", False, "x: list[RecordID]"),
    ("set<object>", False, "x: list[dict[str, Any]]"),
    ("set<number>", False, "x: set[int | float | Decimal]"),
    ("[int, string]", False, "x: tuple[int, str]"),
    ("'a' | 'b'", False, 'x: Literal["a", "b"]'),
    ("'dq' | \"it's\"", False, 'x: Literal["dq", "it\'s"]'),
    ("'a' | \"a\"", False, 'x: Literal["a"]'),
    ("1 | 2", False, "x: Literal[1, 2]"),
    ("-2", False, "x: Literal[-2]"),
    ("true | false", False, "x: Literal[True, False]"),
    ("1.5f | 2", False, "x: float | Literal[2]"),
    ("1.5f", False, "x: float"),
    ("1.5f | 2.5f", False, "x: float"),
    ("1.5dec", False, "x: Decimal"),
    ("string | 'a' | int | 'b'", False, 'x: str | Literal["a", "b"] | int'),
    ("null | int", False, "x: int | None"),
    ("null", False, "x: None"),
    ("int | string", False, "x: int | str"),
    ("int | string", True, "x: int | str | None = None"),
    ("int | any", False, "x: Any = None"),
    ("regex", False, "x: Any = None"),
    ("function", False, "x: Any = None"),
    ("file", False, "x: Any = None"),
    ("file<bucket>", True, "x: Any = None"),
    ("frob", False, "x: Any = None"),
    ("frob<", False, "x: Any = None"),  # I3: the TYPE kept as written
]


class TestTypes:
    """Spec §6.4.4: canonical kind → annotation, one field per row."""

    @pytest.mark.parametrize(("kind", "nullable", "line"), TYPE_ROWS, ids=[f"{k}-{n}" for k, n, _ in TYPE_ROWS])
    def test_row(self, kind: str, nullable: bool, line: str) -> None:
        assert _line_of(_generate(_f("x", kind, nullable=nullable)), "x") == line

    def test_integer_literal_beyond_int_digit_limit(self) -> None:
        assert _line_of(_generate(_f("x", "1" * 5000)), "x") == "x: int"

    def test_every_row_compiles_passes_ruff_and_imports(self, tmp_path: Path) -> None:
        fields = [_f(f"x{index:02}", kind, nullable=nullable) for index, (kind, nullable, _) in enumerate(TYPE_ROWS)]
        source = _quality(
            tmp_path, SchemaState(tables={"kinds": TableState(name="kinds", fields={f.name: f for f in fields})}), "kinds"
        )
        for index, (_, _, line) in enumerate(TYPE_ROWS):
            assert _line_of(source, f"x{index:02}") == f"x{index:02}" + line[1:]


#: Spec M36, M48 / §9.2: the constant defaults as both lines render them (2.x first where they differ), d23 left out;
#: d5, d6, d11, d15 and d25–d30 are held as Python defaults since D25 was amended, d20, d22, d28 and d31 are not.
LITERAL_DEFAULTS: list[tuple[str, str]] = [
    ("DEFINE FIELD d1 ON t TYPE float DEFAULT 0.5f PERMISSIONS FULL", "d1: float = 0.5"),
    ("DEFINE FIELD d2 ON t TYPE decimal DEFAULT 1.5dec PERMISSIONS FULL", 'd2: Decimal = Decimal("1.5")'),
    ("DEFINE FIELD d3 ON t TYPE float DEFAULT 1000f PERMISSIONS FULL", "d3: float = 1000.0"),
    ("DEFINE FIELD d4 ON t TYPE int DEFAULT -2 PERMISSIONS FULL", "d4: int = -2"),
    (
        "DEFINE FIELD d5 ON t TYPE datetime DEFAULT d'2020-01-01T00:00:00Z' PERMISSIONS FULL",
        'd5: datetime = datetime.fromisoformat("2020-01-01T00:00:00Z")',
    ),
    (
        "DEFINE FIELD d6 ON t TYPE duration DEFAULT 1h30m PERMISSIONS FULL",
        'd6: Duration = Field(default_factory=lambda: Duration.parse("1h30m"))',
    ),
    ("DEFINE FIELD d7 ON t TYPE option<int> DEFAULT NONE PERMISSIONS FULL", "d7: int | None = None"),
    ("DEFINE FIELD d7 ON t TYPE none | int DEFAULT NONE PERMISSIONS FULL", "d7: int | None = None"),
    ("DEFINE FIELD d8 ON t TYPE null | int DEFAULT NULL PERMISSIONS FULL", "d8: int | None = None"),
    ("DEFINE FIELD d9 ON t TYPE bool DEFAULT true PERMISSIONS FULL", "d9: bool = True"),
    ('DEFINE FIELD d10 ON t TYPE string DEFAULT "it\'s" PERMISSIONS FULL', 'd10: str = "it\'s"'),
    ("DEFINE FIELD d11 ON t TYPE array<int> DEFAULT [] PERMISSIONS FULL", "d11: list[int] = Field(default_factory=list)"),
    ("DEFINE FIELD d12 ON t TYPE float DEFAULT 1 PERMISSIONS FULL", "d12: float = 1.0"),
    ("DEFINE FIELD d13 ON t TYPE decimal DEFAULT 0.5f PERMISSIONS FULL", "d13: Decimal | None = None"),
    ("DEFINE FIELD d14 ON t TYPE int DEFAULT 9223372036854775807 PERMISSIONS FULL", "d14: int = 9223372036854775807"),
    (
        "DEFINE FIELD d15 ON t TYPE uuid DEFAULT u'0190d3b4-0000-7000-8000-000000000000' PERMISSIONS FULL",
        'd15: UUID = UUID("0190d3b4-0000-7000-8000-000000000000")',
    ),
    ("DEFINE FIELD d16 ON t TYPE float DEFAULT 0.1f PERMISSIONS FULL", "d16: float = 0.1"),
    ("DEFINE FIELD d17 ON t TYPE float DEFAULT -0f PERMISSIONS FULL", "d17: float = -0.0"),
    ("DEFINE FIELD d18 ON t TYPE string DEFAULT 'a\\nb' PERMISSIONS FULL", 'd18: str = "a\\nb"'),
    ("DEFINE FIELD d18 ON t TYPE string DEFAULT 'a\nb' PERMISSIONS FULL", 'd18: str = "a\\nb"'),  # embedded engine
    ("DEFINE FIELD d19 ON t TYPE int DEFAULT 1000 PERMISSIONS FULL", "d19: int = 1000"),
    ("DEFINE FIELD d20 ON t TYPE float DEFAULT math::INF PERMISSIONS FULL", "d20: float | None = None"),
    ("DEFINE FIELD d20 ON t TYPE float DEFAULT math::INFINITY PERMISSIONS FULL", "d20: float | None = None"),
    ("DEFINE FIELD d21 ON t TYPE string DEFAULT 'x' PERMISSIONS FULL", 'd21: str = "x"'),
    ("DEFINE FIELD d22 ON t TYPE record DEFAULT a:1 PERMISSIONS FULL", "d22: RecordID | None = None"),
    ("DEFINE FIELD d24 ON t TYPE float DEFAULT 0.0025f PERMISSIONS FULL", "d24: float = 0.0025"),
    (
        "DEFINE FIELD d25 ON t TYPE array<int> DEFAULT [1, 2] PERMISSIONS FULL",
        "d25: list[int] = Field(default_factory=lambda: [1, 2])",
    ),
    ("DEFINE FIELD d26 ON t TYPE set<string> DEFAULT [] PERMISSIONS FULL", "d26: set[str] = Field(default_factory=set)"),
    ("DEFINE FIELD d27 ON t TYPE object DEFAULT {  } PERMISSIONS FULL", "d27: dict[str, Any] = Field(default_factory=dict)"),
    (
        "DEFINE FIELD d28 ON t TYPE datetime DEFAULT d'2030-01-01T00:00:00.123456789Z' PERMISSIONS FULL",
        "d28: datetime | None = None",
    ),
    (
        "DEFINE FIELD d29 ON t TYPE duration DEFAULT 1h30m5ns PERMISSIONS FULL",
        'd29: Duration = Field(default_factory=lambda: Duration.parse("1h30m5ns"))',
    ),
    (
        "DEFINE FIELD d30 ON t TYPE array<array<int>> DEFAULT [[1], []] PERMISSIONS FULL",
        "d30: list[list[int]] = Field(default_factory=lambda: [[1], []])",
    ),
    ("DEFINE FIELD d31 ON t TYPE set<int> DEFAULT [2, 1, 2] PERMISSIONS FULL", "d31: set[int] | None = None"),
]

#: Spec §9.2 (M48): the sub-model rows, ``p2`` in the 2.x/emb rendering, then the 3.x one.
SUB_MODEL_DEFAULTS: list[tuple[str, ...]] = [
    (
        "DEFINE FIELD p1 ON t TYPE object DEFAULT {  } PERMISSIONS FULL",
        "DEFINE FIELD p1.theme ON t TYPE string DEFAULT 'light' PERMISSIONS FULL",
        "DEFINE FIELD p2 ON t TYPE option<object> DEFAULT {  } PERMISSIONS FULL",
        "DEFINE FIELD p2.theme ON t TYPE string PERMISSIONS FULL",
    ),
    (
        "DEFINE FIELD p1 ON t TYPE object DEFAULT {  } PERMISSIONS FULL",
        "DEFINE FIELD p1.theme ON t TYPE string DEFAULT 'light' PERMISSIONS FULL",
        "DEFINE FIELD p2 ON t TYPE none | object DEFAULT {  } PERMISSIONS FULL",
        "DEFINE FIELD p2.theme ON t TYPE string PERMISSIONS FULL",
    ),
]

_UUID_TEXT = "0190d3b4-0000-7000-8000-000000000000"

#: Further rows of spec §6.4.6's literal-default table: (kind, nullable, DEFAULT text, the generated line).
DEFAULT_ROWS: list[tuple[str, bool, str, str]] = [
    ("number", False, "2", "x: int | float | Decimal = 2"),
    ("number", False, "0.5f", "x: int | float | Decimal = 0.5"),
    ("number", False, "1.5dec", 'x: int | float | Decimal = Decimal("1.5")'),
    ("decimal", False, "2", 'x: Decimal = Decimal("2")'),
    ("float | decimal", False, "2", "x: float | Decimal = 2.0"),
    ("float", False, "1" * 400, "x: float | None = None"),  # float(n) != n
    ("float", False, "1" * 400 + "f", "x: float | None = None"),  # not finite
    ("int", False, "1" * 5000, "x: int | None = None"),  # beyond int()'s digit limit
    ("any", False, "7", "x: Any = 7"),
    ("any", False, "0.5f", "x: Any = 0.5"),
    ("any", False, "'x'", 'x: Any = "x"'),
    ("any", False, "false", "x: Any = False"),
    ("any", False, "NONE", "x: Any = None"),
    ("any", False, "1.5dec", "x: Any = None"),  # dec: Decimal only
    ("any", False, "time::now()", "x: Any = None"),
    ("'a' | 'b'", False, "'a'", 'x: Literal["a", "b"] = "a"'),
    ("'a' | 'b'", False, "'c'", 'x: Literal["a", "b"] | None = None'),
    ("true | 'x'", False, "true", 'x: Literal[True, "x"] = True'),
    ("1 | 2", False, "1", "x: Literal[1, 2] | None = None"),  # int: an int annotation only
    ("bool", False, "1", "x: bool | None = None"),  # int, not via bool
    ("int", False, "true", "x: int | None = None"),
    ("int", False, "NONE", "x: int | None = None"),  # NONE: the annotation must hold None
    ("int", True, "NULL", "x: int | None = None"),
    ("int", True, "18", "x: int | None = 18"),
    ("string", False, "string::concat('a', 'b')", "x: str | None = None"),
    ("string", False, "{  }", "x: str | None = None"),
    ("geometry<point>", True, "(1, 2)", "x: PointField = None"),
    ("geometry<point>", False, "(1, 2)", "x: GeometryPoint | None = None"),
    # D25 amended (spec §6.4.6 item 3, M48): [] on a list, a set, Any; a set written list[…]; a tuple keeps the server form.
    ("array<int>", True, "[]", "x: list[int] | None = Field(default_factory=list)"),
    ("array", False, "[ ]", "x: list[Any] = Field(default_factory=list)"),
    ("set<int>", True, "[]", "x: set[int] | None = Field(default_factory=set)"),
    ("set<record>", False, "[]", "x: list[RecordID] = Field(default_factory=list)"),
    ("any", False, "[]", "x: Any = Field(default_factory=list)"),
    ("[int, string]", False, "[]", "x: tuple[int, str] | None = None"),
    ("string", False, "[]", "x: str | None = None"),
    # [e1, …] on one array kind whose item annotation holds every element, written with that row's expression.
    ("array<float>", False, "[1.5f, 2]", "x: list[float] = Field(default_factory=lambda: [1.5, 2.0])"),
    (
        "array<decimal>",
        False,
        "[1.5dec, 2]",
        'x: list[Decimal] = Field(default_factory=lambda: [Decimal("1.5"), Decimal("2")])',
    ),
    ("array<string>", False, "['a', \"b'c\"]", 'x: list[str] = Field(default_factory=lambda: ["a", "b\'c"])'),
    ("array<bool>", True, "[true]", "x: list[bool] | None = Field(default_factory=lambda: [True])"),
    ("array<option<int>>", False, "[1, NONE]", "x: list[int | None] = Field(default_factory=lambda: [1, None])"),
    ("array<'a' | 'b'>", False, "['b']", 'x: list[Literal["a", "b"]] = Field(default_factory=lambda: ["b"])'),
    ("array", False, "[[1, 'x'], NONE, {  }]", 'x: list[Any] = Field(default_factory=lambda: [[1, "x"], None, {}])'),
    ("any", False, "[1, [2]]", "x: Any = Field(default_factory=lambda: [1, [2]])"),
    ("array<object>", False, "[{  }, {}]", "x: list[dict[str, Any]] = Field(default_factory=lambda: [{}, {}])"),
    ("array<set<int>>", False, "[[]]", "x: list[set[int]] = Field(default_factory=lambda: [set()])"),
    (
        "array<duration>",
        False,
        "[1h, 0ns]",
        'x: list[Duration] = Field(default_factory=lambda: [Duration.parse("1h"), Duration.parse("0ns")])',
    ),
    (
        "array<datetime>",
        False,
        "[d'2030-01-01T00:00:00Z']",
        'x: list[datetime] = Field(default_factory=lambda: [datetime.fromisoformat("2030-01-01T00:00:00Z")])',
    ),
    ("array<uuid>", False, f"[u'{_UUID_TEXT}']", f'x: list[UUID] = Field(default_factory=lambda: [UUID("{_UUID_TEXT}")])'),
    ("array<int> | string", False, "[1]", "x: list[int] | str = Field(default_factory=lambda: [1])"),
    ("array<int> | array<string>", False, "[]", "x: list[int] | list[str] = Field(default_factory=list)"),
    ("array<int> | array<string>", False, "[1]", "x: list[int] | list[str] | None = None"),  # two array members
    ("set<int>", False, "[1]", "x: set[int] | None = None"),  # the server reorders and deduplicates a set
    ("set<object>", False, "[{  }]", "x: list[dict[str, Any]] | None = None"),  # a set, written list[…]
    ("[int, string]", False, "[1, 'a']", "x: tuple[int, str] | None = None"),
    ("array<int>", False, "[1, 'a']", "x: list[int] | None = None"),  # an element of another type
    ("array<int>", False, "[1, time::now()]", "x: list[int] | None = None"),
    ("array<int>", False, "[1] + [2]", "x: list[int] | None = None"),  # not one array literal
    ("array<int>", False, "[1,]", "x: list[int] | None = None"),  # an empty element
    ("array<int>", False, "[1, 'unterminated]", "x: list[int] | None = None"),  # not scannable
    ("array<array<int>>", False, "[[1], 2]", "x: list[list[int]] | None = None"),
    ("array<geometry<point>>", False, "[(1, 2)]", "x: list[GeometryPoint] | None = None"),
    ("array", False, "[" * 32 + "]" * 32, "x: list[Any] = Field(default_factory=lambda: " + "[" * 32 + "]" * 32 + ")"),
    ("array", False, "[" * 33 + "]" * 33, "x: list[Any] | None = None"),  # nested deeper than 32 arrays
    # {  } on a dict, an object literal kind or Any; a non-empty object keeps the server form.
    ("object", True, "{}", "x: dict[str, Any] | None = Field(default_factory=dict)"),
    ("{ a: int }", False, "{ }", "x: dict[str, Any] = Field(default_factory=dict)"),
    ("any", False, "{  }", "x: Any = Field(default_factory=dict)"),
    ("object", False, "{ a: 1 }", "x: dict[str, Any] | None = None"),
    # Durations Duration.parse() accepts, on a Duration or Any.
    ("duration", True, "1h", 'x: Duration | None = Field(default_factory=lambda: Duration.parse("1h"))'),
    ("any", False, "2w3d", 'x: Any = Field(default_factory=lambda: Duration.parse("2w3d"))'),
    ("duration", False, "1.5h", "x: Duration | None = None"),
    ("duration", False, "1H", "x: Duration | None = None"),
    ("string", False, "1h", "x: str | None = None"),
    # Datetimes: UTC, at most 6 fractional digits, a date that exists.
    ("datetime", False, "d'2030-01-01T00:00:00.5Z'", 'x: datetime = datetime.fromisoformat("2030-01-01T00:00:00.5Z")'),
    (
        "datetime",
        True,
        "d'2030-01-01T00:00:00.123456Z'",
        'x: datetime | None = datetime.fromisoformat("2030-01-01T00:00:00.123456Z")',
    ),
    ("any", False, "d'2030-01-01T00:00:00Z'", 'x: Any = datetime.fromisoformat("2030-01-01T00:00:00Z")'),
    ("datetime", False, "d'2030-01-01T00:00:00.1234567Z'", "x: datetime | None = None"),
    ("datetime", False, "d'2030-02-30T00:00:00Z'", "x: datetime | None = None"),
    ("datetime", False, "d'2030-01-01T00:00:00+02:00'", "x: datetime | None = None"),
    ("datetime", False, "'2030-01-01T00:00:00Z'", "x: datetime | None = None"),
    ("string", False, "d'2030-01-01T00:00:00Z'", "x: str | None = None"),
    # UUIDs in the canonical form.
    ("uuid", True, f"u'{_UUID_TEXT}'", f'x: UUID | None = UUID("{_UUID_TEXT}")'),
    ("any", False, f"u'{_UUID_TEXT}'", f'x: Any = UUID("{_UUID_TEXT}")'),
    ("uuid", False, f"u'{_UUID_TEXT.upper()}'", "x: UUID | None = None"),
    ("uuid", False, "u'x'", "x: UUID | None = None"),
    ("uuid", False, "u''", "x: UUID | None = None"),
    ("string", False, f"u'{_UUID_TEXT}'", "x: str | None = None"),
]


class TestFieldLines:
    """Spec §6.4.6: computed fields and their fallback, writable VALUEs, literal defaults, ``server_fields`` by
    attribute name, aliases, ``READONLY``."""

    def test_computed(self) -> None:
        source = _generate(_f("x", "int", computed="a * 2"), _f("y", "any", computed="a * 3"))
        assert _line_of(source, "x") == 'x: Computed[int] = computed("a * 2")'
        assert _line_of(source, "y") == 'y: Computed[Any] = computed("a * 3")'
        assert "server_fields" not in source

    def test_computed_nullable_kind(self) -> None:
        source = _generate(
            _f("x", "int", nullable=True, computed="a * 2"), _f("g", "geometry<point>", nullable=True, computed="b")
        )
        assert _line_of(source, "x") == 'x: Computed[int] = computed("a * 2")'
        assert _line_of(source, "g") == 'g: Computed[GeometryPoint] = computed("b")'

    def test_value_without_value_or_input_is_computed(self) -> None:
        source = _generate(
            _f("a", "datetime", value="time::now()"),
            _f("b", "string", value="string::concat('$value', c)"),  # $value inside a string: not a reference
            _f("c", "int", value="$values + 1"),  # another parameter
            _f("d", "string", value="IF $x THEN 'a' ELSE 'b' END"),
        )
        assert _line_of(source, "a") == 'a: Computed[datetime] = computed("time::now()")'
        assert _line_of(source, "b") == "b: Computed[str] = computed(\"string::concat('$value', c)\")"
        assert _line_of(source, "c") == 'c: Computed[int] = computed("$values + 1")'
        assert _line_of(source, "d") == "d: Computed[str] = computed(\"IF $x THEN 'a' ELSE 'b' END\")"

    def test_writable_value(self) -> None:
        source = _generate(
            _f("a", "string", value="string::lowercase($value)"),
            _f("b", "int", nullable=True, value="$input * 2"),
            _f("c", "string", value="{ RETURN $value.trim(); }", default="'x'"),
            _f("d", "string", value="'unterminated $value"),  # not scannable: the text is searched
        )
        assert _line_of(source, "a") == "a: str"
        assert _line_of(source, "b") == "b: int | None = None"
        assert _line_of(source, "c") == 'c: str = "x"'
        assert _line_of(source, "d") == "d: str"
        assert "server_fields" not in source and "Computed" not in source

    def test_computed_fallback_to_server_form(self) -> None:
        source = _generate(
            _f("x", "string", value="string::concat(a, ';', b)"),
            _f("e", "int", value=" "),
            _f("first-name", "int", computed="a + 1"),
        )
        assert _line_of(source, "x") == "x: str | None = None"
        assert _line_of(source, "e") == "e: int | None = None"
        assert _line_of(source, "first_name") == 'first_name: int | None = Field(default=None, alias="first-name")'
        assert _model_config(source, "T") == (
            '    model_config = SurrealConfigDict(table_name="t", server_fields=["e", "first_name", "x"])'
        )
        assert "computed(" not in source

    @pytest.mark.parametrize(
        "statement",
        [
            "DEFINE FIELD blk ON t VALUE {\nLET $x = 1;\nRETURN $x + 1;\n} PERMISSIONS FULL",
            "DEFINE FIELD blk ON t VALUE { LET $x = 1; RETURN $x + 1; } PERMISSIONS FULL",
        ],
    )
    def test_block_value_takes_the_server_form(self, statement: str) -> None:
        source = _generate(parse_define_field(statement).state)
        assert _line_of(source, "blk") == "blk: Any = None"
        assert _model_config(source, "T") == '    model_config = SurrealConfigDict(table_name="t", server_fields=["blk"])'

    @pytest.mark.parametrize(("statement", "line"), LITERAL_DEFAULTS, ids=[line.split(":")[0] for _, line in LITERAL_DEFAULTS])
    def test_literal_defaults_m36(self, statement: str, line: str) -> None:
        field = parse_define_field(statement).state
        source = _generate(field)
        assert _line_of(source, field.name) == line
        server = line.endswith("| None = None") and field.name not in ("d7", "d8")
        assert ("server_fields=" in source) is server

    def test_literal_defaults_server_fields_in_field_line_order(self) -> None:
        fields = [parse_define_field(statement).state for statement, _ in LITERAL_DEFAULTS]
        source = _generate(*{field.name: field for field in fields}.values())
        assert _model_config(source, "T") == (
            '    model_config = SurrealConfigDict(table_name="t", server_fields=["d13", "d20", "d22", "d28", "d31"])'
        )

    @pytest.mark.parametrize(("kind", "nullable", "default", "line"), DEFAULT_ROWS)
    def test_default_rows(self, kind: str, nullable: bool, default: str, line: str) -> None:
        assert _line_of(_generate(_f("x", kind, nullable=nullable, default=default)), "x") == line

    def test_default_rows_compile_pass_ruff_and_import(self, tmp_path: Path) -> None:
        fields = [
            _f(f"x{index:02}", kind, nullable=nullable, default=default)
            for index, (kind, nullable, default, _) in enumerate(DEFAULT_ROWS)
        ]
        source = _quality(
            tmp_path, SchemaState(tables={"rows": TableState(name="rows", fields={f.name: f for f in fields})}), "rows"
        )
        for index, (_, _, _, line) in enumerate(DEFAULT_ROWS):
            assert _line_of(source, f"x{index:02}") == f"x{index:02}" + line[1:]

    def test_clauses_after_a_regex_or_a_graph_wildcard_m47(self) -> None:
        # The parser ends the expression at the regex or the wildcard: the clauses after it are not generated into
        # the field's default or computed expression.
        statements = [
            "DEFINE FIELD k ON t TYPE string DEFAULT 'x' PERMISSIONS FOR select WHERE $auth.email = /@corp$/, FOR create, "
            "update FULL",
            "DEFINE FIELD fans ON t VALUE <-likes<-? PERMISSIONS FULL",
            "DEFINE FIELD code ON t TYPE string ASSERT $value = /^[a-z]+$/ COMMENT 'lower only' PERMISSIONS FULL",
        ]
        source = _generate(*(parse_define_field(statement).state for statement in statements))
        assert _line_of(source, "k") == 'k: str = "x"'
        assert _line_of(source, "fans") == 'fans: Computed[Any] = computed("<-likes<-?")'
        assert _line_of(source, "code") == "code: str"
        assert "server_fields" not in source and "PERMISSIONS" not in source

    def test_server_fields_hold_attribute_names(self) -> None:
        created = parse_define_field("DEFINE FIELD `created-at` ON t TYPE datetime DEFAULT time::now() PERMISSIONS FULL")
        source = _generate(created.state, _f("x", "string", value="string::concat(a, ';', b)"))
        assert _line_of(source, "created_at") == 'created_at: datetime | None = Field(default=None, alias="created-at")'
        assert _line_of(source, "x") == "x: str | None = None"
        assert _model_config(source, "T") == (
            '    model_config = SurrealConfigDict(table_name="t", server_fields=["created_at", "x"])'
        )

    def test_readonly_comment(self) -> None:
        source = _generate(
            _f("a", "int", default="0", readonly=True),
            _f("b", "int", computed="c", readonly=True),
            _f("first-name", "string", readonly=True),
        )
        assert _line_of(source, "a") == "a: int = 0  # READONLY"
        assert _line_of(source, "b") == 'b: Computed[int] = computed("c")  # READONLY'
        assert _line_of(source, "first_name") == 'first_name: str = Field(alias="first-name")  # READONLY'

    def test_alias_forms(self) -> None:
        source = _generate(
            _f("first-name"),
            _f("x-y", "int", default="18"),
            _f("a-b", "int", nullable=True),
            _f("class", "string", default="'k'"),
        )
        assert _line_of(source, "first_name") == 'first_name: str = Field(alias="first-name")'
        assert _line_of(source, "x_y") == 'x_y: int = Field(default=18, alias="x-y")'
        assert _line_of(source, "a_b") == 'a_b: int | None = Field(default=None, alias="a-b")'
        assert _line_of(source, "class_") == 'class_: str = Field(default="k", alias="class")'

    def test_alias_forms_of_constant_defaults(self) -> None:
        """Spec §6.4.6 item 5: a factory default with an alias, and the plain forms of a datetime and a UUID."""
        source = _generate(
            _f("a-b", "array<int>", default="[1]"),
            _f("c-d", "datetime", default="d'2030-01-01T00:00:00Z'"),
            _f("e-f", "uuid", nullable=True, default=f"u'{_UUID_TEXT}'"),
            _f("g-h", "object", default="{  }"),
            _f("i-j", "set<int>", default="[]"),
        )
        assert _line_of(source, "a_b") == 'a_b: list[int] = Field(default_factory=lambda: [1], alias="a-b")'
        assert _line_of(source, "c_d") == (
            'c_d: datetime = Field(default=datetime.fromisoformat("2030-01-01T00:00:00Z"), alias="c-d")'
        )
        assert _line_of(source, "e_f") == f'e_f: UUID | None = Field(default=UUID("{_UUID_TEXT}"), alias="e-f")'
        assert _line_of(source, "g_h") == 'g_h: dict[str, Any] = Field(default_factory=dict, alias="g-h")'
        assert _line_of(source, "i_j") == 'i_j: set[int] = Field(default_factory=set, alias="i-j")'
        assert "server_fields" not in source

    def test_ignored_clauses(self) -> None:
        field = _f(
            "x",
            "record<a>",
            assertion="$value != NONE",
            permissions={"select": "FULL", "create": "NONE", "update": "NONE"},
            reference=True,
            on_delete="CASCADE",
            comment="a comment",
        )
        source = _generate(field)
        assert _line_of(source, "x") == "x: RecordID"
        assert "a comment" not in source and "CASCADE" not in source and "!=" not in source

    def test_field_lines_in_codepoint_order_id_first(self) -> None:
        source = _generate(_f("b"), _f("a"), _f("B"), _f("_c"), _f("id", "record"))
        body = [line.strip() for line in _class_block(source, "T").splitlines()[3:]]
        assert body == ["id: str | None = None", "B: str", 'c: str = Field(alias="_c")', "a: str", "b: str"]


class TestSubModels:
    """Spec §6.4.5: declared nested fields become sub-models; otherwise ``dict[str, Any]``."""

    def test_required_and_nullable_object(self) -> None:
        source = _generate(
            _f("address", "object"),
            _f("address.city"),
            _f("address.zip", "int", nullable=True),
            _f("billing", "object", nullable=True),
            _f("billing.city"),
        )
        assert _class_block(source, "TAddress") == "class TAddress(BaseModel):\n    city: str\n    zip: int | None = None\n"
        assert _class_block(source, "TBilling") == "class TBilling(BaseModel):\n    city: str\n"
        assert _line_of(_class_block(source, "T"), "address") == "address: TAddress"
        assert _line_of(_class_block(source, "T"), "billing") == "billing: TBilling | None = None"
        assert source.index("class TAddress(") < source.index("class TBilling(") < source.index("class T(")

    def test_flexible_and_schemaless(self) -> None:
        flexible = _generate(_f("meta", "object", flexible=True), _f("meta.a", "int"))
        assert _class_block(flexible, "TMeta") == (
            'class TMeta(BaseModel):\n    model_config = ConfigDict(extra="allow")\n\n    a: int\n'
        )
        schemaless = _generate(_f("meta", "object"), _f("meta.a", "int"), schema_mode="SCHEMALESS")
        assert _class_block(schemaless, "TMeta") == _class_block(flexible, "TMeta")
        assert 'extra="allow")' in _model_config(schemaless, "T")
        plain = _generate(_f("meta", "object"), _f("meta.a", "int"))
        assert _class_block(plain, "TMeta") == "class TMeta(BaseModel):\n    a: int\n"
        assert "from pydantic import BaseModel\n" in plain

    def test_nested_two_levels(self) -> None:
        source = _generate(
            _f("address", "object"),
            _f("address.city"),
            _f("address.geo", "object", nullable=True),
            _f("address.geo.lat", "float"),
            _f("address.geo.lng", "float", default="0.5f"),
        )
        assert _class_block(source, "TAddressGeo") == "class TAddressGeo(BaseModel):\n    lat: float\n    lng: float = 0.5\n"
        assert _class_block(source, "TAddress") == (
            "class TAddress(BaseModel):\n    city: str\n    geo: TAddressGeo | None = None\n"
        )
        assert source.index("class TAddressGeo(") < source.index("class TAddress(") < source.index("class T(")

    def test_array_of_objects(self) -> None:
        source = _generate(
            _f("w", "array<object>"),
            _f("w.*", "object"),
            _f("w.*.z", "int"),
            _f("v", "array<object>", nullable=True),
            _f("v.*.q"),
        )
        assert _line_of(source, "w") == "w: list[TWItem]"
        assert _line_of(source, "v") == "v: list[TVItem] | None = None"
        assert _class_block(source, "TWItem") == "class TWItem(BaseModel):\n    z: int\n"
        assert _class_block(source, "TVItem") == "class TVItem(BaseModel):\n    q: str\n"

    @pytest.mark.parametrize("child", ["first-name", "datetime", "l", "save", "class", "_x", "T"])
    def test_child_that_needs_an_alias_or_is_reserved(self, child: str) -> None:
        source = _generate(_f("address", "object"), _f("address.city"), _f("address." + child, path=["address", child]))
        assert _line_of(source, "address") == "address: dict[str, Any]"
        assert "class TAddress(" not in source
        items = _generate(_f("w", "array<object>", nullable=True), _f("w.*." + child, path=["w", "*", child]))
        assert _line_of(items, "w") == "w: list[dict[str, Any]] | None = None"

    def test_undefined_parent_s5(self) -> None:
        source = _generate(_f("a.b", "int"), _f("a.c"), _f("tags.*"), _f("first-name.x", path=["first-name", "x"]))
        assert _line_of(source, "a") == "a: dict[str, Any] | None = None"
        assert _line_of(source, "tags") == "tags: dict[str, Any] | None = None"
        assert _line_of(source, "first_name") == 'first_name: dict[str, Any] | None = Field(default=None, alias="first-name")'
        assert source.split("\n\n\n")[0].endswith(
            "# Not generated:\n"
            "# - field 't.a.b': its parent is not defined (the parent is a dict[str, Any])\n"
            "# - field 't.a.c': its parent is not defined (the parent is a dict[str, Any])\n"
            "# - field 't.first-name.x': its parent is not defined (the parent is a dict[str, Any])\n"
            "# - field 't.tags.*': its parent is not defined (the parent is a dict[str, Any])"
        )

    def test_sub_model_fields_have_no_computed_and_no_server_fields(self) -> None:
        source = _generate(
            _f("address", "object"),
            _f("address.at", "datetime", value="time::now()"),
            _f("address.n", "int", computed="1 + 1"),
            _f("address.since", "datetime", default="time::now()"),
            _f("address.kind", "'a' | 'b'", default="'a'"),
            _f("address.g", "geometry<point>", nullable=True),
            _f("address.x", "frob<"),  # I3
        )
        assert _class_block(source, "TAddress") == (
            "class TAddress(BaseModel):\n"
            "    at: datetime | None = None\n"
            "    g: GeometryPoint | None = None\n"
            '    kind: Literal["a", "b"] = "a"\n'
            "    n: int | None = None\n"
            "    since: datetime | None = None\n"
            "    x: Any = None\n"
        )
        assert "server_fields" not in source and "Computed" not in source

    def test_empty_object_default_on_a_sub_model(self) -> None:
        """Spec §6.4.6 item 3 (D25 amended): ``{  }`` on a sub-model every field of which has a default → the class as
        ``default_factory``; the same for ``[{  }]`` on items with a sub-model, written ``<Item>()``."""
        source = _generate(
            _f("p", "object", default="{  }"),
            _f("p.theme", default="'light'"),
            _f("p.n", "object", nullable=True, default="{}"),
            _f("p.n.z", "int", default="4"),
            _f("p.note", nullable=True),
            _f("w", "array<object>", default="[{  }]"),
            _f("w.*.k", "int", default="7"),
        )
        assert _class_block(source, "TPN") == "class TPN(BaseModel):\n    z: int = 4\n"
        assert _class_block(source, "TP") == (
            "class TP(BaseModel):\n"
            "    n: TPN | None = Field(default_factory=TPN)\n"
            "    note: str | None = None\n"
            '    theme: str = "light"\n'
        )
        model = _class_block(source, "T")
        assert _line_of(model, "p") == "p: TP = Field(default_factory=TP)"
        assert _line_of(model, "w") == "w: list[TWItem] = Field(default_factory=lambda: [TWItem()])"
        assert "server_fields" not in source and "update() after save()" not in source  # every DEFAULT is held

    def test_empty_object_default_on_a_sub_model_needing_arguments(self) -> None:
        """M48: the server refuses every create that leaves such an object to its ``DEFAULT`` — the field is required,
        its annotation unchanged, nullable or not; an item that needs arguments keeps the array in the server form."""
        source = _generate(
            _f("p", "object", nullable=True, default="{  }"),
            _f("p.theme"),
            _f("q", "object", default="{  }"),
            _f("q.r", "object", default="{  }"),
            _f("q.r.s", "int"),
            _f("w", "array<object>", default="[{  }]"),
            _f("w.*.k", "int"),
        )
        model = _class_block(source, "T")
        assert _line_of(model, "p") == "p: TP | None"
        assert _class_block(source, "TQ") == "class TQ(BaseModel):\n    r: TQR\n"
        assert _line_of(model, "q") == "q: TQ"
        assert _line_of(model, "w") == "w: list[TWItem] | None = None"
        assert _model_config(source, "T") == '    model_config = SurrealConfigDict(table_name="t", server_fields=["w"])'
        assert "update() after save()" not in source

    def test_item_definitions_produce_no_attribute(self) -> None:
        source = _generate(_f("tags", "array<string>"), _f("tags.*"), _f("w", "array<int>"), _f("w.*", "int"))
        body = [line.strip() for line in _class_block(source, "T").splitlines()[3:]]
        assert body == ["id: str | None = None", "tags: list[str]", "w: list[int]"]
        assert "Not generated" not in source


class TestClasses:
    """Spec §6.4.7: class blocks — base class, comments, ``model_config``, ``id``."""

    def test_edge(self) -> None:
        source = _generate(
            _f("in", "record<a>"),
            _f("out", "record<b>"),
            _f("id", "record"),
            _f("since", "datetime", nullable=True),
            name="e",
            table_type="relation",
            relation_in=(),
            relation_out=["b", "c"],
            enforced=True,
        )
        assert _class_block(source, "E") == (
            "class E(BaseSurrealEdge):\n"
            "    # IN any OUT b | c ENFORCED\n"
            '    model_config = SurrealConfigDict(table_name="e")\n'
            "\n"
            "    since: datetime | None = None\n"
        )
        bare = _generate(name="r", table_type="relation", relation_in="a | b", schema_mode="SCHEMALESS")
        assert _class_block(bare, "R") == (
            "class R(BaseSurrealEdge):\n"
            "    # IN a | b OUT any\n"
            '    # SCHEMALESS: fields not declared here are kept (extra="allow")\n'
            '    model_config = SurrealConfigDict(table_name="r", extra="allow")\n'
        )

    @pytest.mark.parametrize("table_type", ["relation", "RELATION", "Relation"])
    def test_edge_whatever_the_case_of_its_type(self, tmp_path: Path, table_type: str) -> None:
        """A hand-built ``TableState`` may spell its type in any case: the class is an edge either way, and ``in``/``out``
        stay the base class's (declared again, the module failed to import with a ``TypeError``)."""
        likes = _table(
            "likes",
            _f("in", "record<person>"),
            _f("out", "record<post>"),
            _f("since", "datetime", nullable=True),
            table_type=table_type,
        )
        source = ModelCodeGenerator().generate(SchemaState(tables={"likes": likes}))
        module = _import(tmp_path, source)
        assert issubclass(module.Likes, surreal_orm_lite.BaseSurrealEdge)
        assert set(module.Likes.__annotations__) == {"since"}
        assert {"in_", "out", "since"} <= set(module.Likes.model_fields)
        assert _class_block(source, "Likes") == (
            "class Likes(BaseSurrealEdge):\n"
            "    # IN any OUT any\n"
            '    model_config = SurrealConfigDict(table_name="likes")\n'
            "\n"
            "    since: datetime | None = None\n"
        )
        assert dict(_Names([likes]).fields("likes")) == {"since": ("since", None)}

    def test_changefeed_and_permissions(self) -> None:
        """The table's change feed and permissions are class comments, the permissions grouped as SurrealDB renders
        them (the same text on 2.x, 3.x and the embedded engine)."""
        table = _parsed_table(
            "DEFINE TABLE feed TYPE NORMAL SCHEMAFULL CHANGEFEED 1d INCLUDE ORIGINAL PERMISSIONS FOR select WHERE "
            "$auth.id = owner, FOR create, update FULL, FOR delete NONE",
            ["DEFINE FIELD owner ON feed TYPE option<record> PERMISSIONS FULL"],
        )
        source = ModelCodeGenerator().generate(SchemaState(tables={"feed": table}))
        assert _class_block(source, "Feed") == (
            "class Feed(BaseSurrealModel):\n"
            "    # Changefeed: 1d INCLUDE ORIGINAL\n"
            "    # Permissions: FOR select WHERE $auth.id = owner, FOR create, update FULL, FOR delete NONE\n"
            '    model_config = SurrealConfigDict(table_name="feed")\n'
            "\n"
            "    id: str | None = None\n"
            "    owner: RecordID | None = None\n"
        )

    @pytest.mark.parametrize(
        ("changefeed", "include_original", "permissions", "expected"),
        [
            ("3h", False, _TABLE_PERMISSIONS_NONE, ["# Changefeed: 3h"]),
            (None, False, _TABLE_PERMISSIONS_NONE, []),
            (None, False, {}, []),
            (None, False, dict.fromkeys(_TABLE_PERMISSIONS_NONE, "FULL"), ["# Permissions: FULL"]),
            (None, False, dict.fromkeys(_TABLE_PERMISSIONS_NONE, "WHERE a > 1"), ["# Permissions: WHERE a > 1"]),
            (None, False, {"select": "FULL"}, ["# Permissions: FOR select FULL"]),
            (
                None,
                False,
                {"delete": "FULL", "update": "NONE", "create": "WHERE a, b", "select": "WHERE a, b"},
                ["# Permissions: FOR select, create WHERE a, b, FOR update NONE, FOR delete FULL"],
            ),
            (
                "1w",
                True,
                {"select": "FULL", "create": "NONE", "update": "FULL", "delete": "NONE"},
                ["# Changefeed: 1w INCLUDE ORIGINAL", "# Permissions: FOR select, update FULL, FOR create, delete NONE"],
            ),
        ],
        ids=["changefeed", "defaults", "no-permissions", "full", "where", "partial", "grouped", "both"],
    )
    def test_changefeed_and_permissions_comments(
        self, changefeed: str | None, include_original: bool, permissions: dict[str, str], expected: list[str]
    ) -> None:
        """``PERMISSIONS NONE`` on every operation, the default of a table, writes no comment; one rule for all four
        operations is written alone, as SurrealDB renders it."""
        source = _generate(
            changefeed=changefeed, changefeed_include_original=include_original, permissions=permissions, comment="c"
        )
        lines = _class_block(source, "T").splitlines()
        assert lines[1] == "    # Comment: c"
        assert lines[2 : 2 + len(expected)] == ["    " + line for line in expected]
        assert lines[2 + len(expected)].startswith("    model_config = ")

    def test_long_permissions_are_cut(self) -> None:
        source = _generate(permissions={"select": "WHERE " + "a" * 300})
        line = _class_block(source, "T").splitlines()[1]
        assert line == "    # Permissions: FOR select WHERE " + "a" * 89 + "..."
        assert len(line) == 128

    def test_comments(self) -> None:
        source = _generate(
            _f("a", "int"),
            name="v1",
            comment="it's a view",
            view_query="SELECT count() AS total FROM src GROUP ALL",
            indexes={
                "by_a": IndexState(name="by_a", fields=("a",), unique=True),
                "ft": IndexState(name="ft", fields=("a",), search_analyzer="ascii"),
                "hn": IndexState(name="hn", fields=("a",), hnsw=True),
                "mt": IndexState(name="mt", fields=("a",), mtree=True),
                "cnt": IndexState(name="cnt", count=True),
                "plain": IndexState(name="plain", fields=("a",)),
            },
            events={
                "e2": EventState("e2", "v1", "true", "(x)"),
                "e1": EventState("e1", "v1", "true", "(x)"),
            },
        )
        assert _class_block(source, "V1").splitlines()[:5] == [
            "class V1(BaseSurrealModel):",
            "    # Comment: it's a view",
            "    # View: AS SELECT count() AS total FROM src GROUP ALL",
            "    # Indexes: by_a (UNIQUE), cnt (COUNT), ft (FULLTEXT), hn (HNSW), mt (MTREE), plain",
            "    # Events: e1, e2",
        ]

    def test_long_comment_is_cut(self) -> None:
        source = _generate(comment="z" * 300)
        line = _class_block(source, "T").splitlines()[1]
        assert line == "    # Comment: " + "z" * 110 + "..."
        assert len(line) == 128

    @pytest.mark.parametrize(
        "fields",
        [
            [_f("tags", "set<datetime>", readonly=True)],
            [_f("tags", "option<set<datetime>>", readonly=True)],
            [_f("w", "array<set<datetime>>", readonly=True)],
            [_f("address", "object"), _f("address.at", "set<datetime>", readonly=True)],
            [_f("union", "string | set<datetime>", readonly=True)],
            [_f("dates", "array<datetime>", readonly=True)],
            [_f("dates", "option<array<datetime, 3>>", readonly=True)],
            [_f("w", "array<object>"), _f("w.*.at", "option<datetime>", readonly=True)],
            [_f("w", "array<object>", readonly=True), _f("w.*.at", "datetime")],  # a READONLY parent's sub-field
            [
                _f("a", "object"),
                _f("a.b", "object"),
                _f("a.b.c", "object"),
                _f("a.b.c.d", "object"),
                _f("a.b.c.d.at", "datetime", readonly=True),
            ],
            [_f("a", "object"), _f("a.b", "object"), _f("a.b.c", "{ d: { at: datetime } }", readonly=True)],
            # #261 review: below the top level, what update() keeps depends on the fields before it (a list of
            # timestamped items is enough to use the budget up), so a sub-object's or a tuple's datetime says so too.
            [_f("address", "object"), _f("address.at", "datetime", readonly=True)],
            [_f("address", "object", readonly=True), _f("address.at", "option<datetime>")],
            [_f("pair", "[int, datetime]", readonly=True)],
            [_f("lit", "{ at: datetime }", readonly=True)],
            [_f("a", "object"), _f("a.b", "object"), _f("a.b.c", "object"), _f("a.b.c.at", "datetime", readonly=True)],
            [_f("a", "object"), _f("a.b", "object"), _f("a.b.c", "{ at: datetime }", readonly=True)],
            [_f("history", "array<object>"), _f("history.*.at", "datetime"), *_META_AT],
        ],
        ids=[
            "set",
            "optional-set",
            "array-of-sets",
            "nested-set",
            "union-set",
            "array",
            "sized-array",
            "array-item",
            "readonly-parent",
            "four-levels",
            "four-levels-literal",
            "sub-object",
            "readonly-object",
            "tuple",
            "literal-object",
            "three-levels",
            "three-levels-literal",
            "after-a-timestamped-array",
        ],
    )
    def test_update_note(self, fields: list[FieldState]) -> None:
        """#261: a ``READONLY`` field holding a datetime ``update()`` may not keep — anywhere below the top level: in a
        sub-object, a tuple, a set or an array — still says so."""
        source = _generate(*fields)
        assert _class_block(source, "T").splitlines()[1] == UPDATE_NOTE
        assert len(UPDATE_NOTE) == 124

    @pytest.mark.parametrize(
        ("count", "note"),
        [(_KEEP_LIMIT, False), (_KEEP_LIMIT + 1, True)],
    )
    @pytest.mark.parametrize("kind", ["datetime", "option<datetime>", "string | datetime", "any"])
    def test_update_note_past_the_kept_top_level_datetimes(self, count: int, note: bool, kind: str) -> None:
        """#261 review: ``update()`` keeps the first ``_KEEP_LIMIT`` top-level datetimes only, so a class with more
        top-level fields that may hold one says so when one of them is ``READONLY`` (here the last, ``d99``)."""
        fields = [_f(f"d{index:02}", kind) for index in range(count - 1)] + [_f("d99", "datetime", readonly=True)]
        assert (UPDATE_NOTE in _generate(*fields).splitlines()) is note
        assert "update()" not in _generate(*fields[:-1], _f("d99", "datetime"), _f("n", "int"))

    @pytest.mark.parametrize(
        "fields",
        [
            [_f("n", "int", readonly=True)],
            [_f("created", "datetime")],
            [_f("label", "'datetime'", readonly=True)],
            [_f("id", "datetime", readonly=True)],
            [_f("pair", "[int, string]", readonly=True), _f("lit", "{ at: int }", readonly=True)],
            [_f("bad", "datetime<", readonly=True)],  # I3: the kind is not known
            [_f("tags", "set<string>", readonly=True)],
            # #261: update() keeps every stored top-level datetime it did not change, up to _KEEP_LIMIT, whatever the
            # fields before it hold.
            [_f("created", "datetime", readonly=True, default="time::now()")],
            [_f("created", "datetime", nullable=True, readonly=True)],
            [_f("created", "string | datetime", readonly=True)],
            [_f("dates", "array<datetime>"), _f("created", "datetime", readonly=True)],  # the array is not READONLY
            [_f("history", "array<object>"), _f("history.*.at", "datetime"), _f("created", "datetime", readonly=True)],
            [_f("address", "object"), _f("address.at", "datetime"), _f("address.n", "int", readonly=True)],
        ],
    )
    def test_no_update_note(self, fields: list[FieldState]) -> None:
        assert "update()" not in _generate(*fields)

    @pytest.mark.parametrize(
        "fields",
        [
            [_f("prefs", "object"), _f("prefs.at", "datetime", default="time::now()")],
            [_f("prefs", "object", nullable=True), _f("prefs.seen", "datetime", nullable=True, default="time::now()")],
            [_f("prefs", "object"), _f("prefs.inner", "object"), _f("prefs.inner.at", "datetime", default="time::now()")],
            [_f("p", "object", default="{  }"), _f("p.at", "datetime", default="time::now()")],
            [_f("w", "array<object>", nullable=True), _f("w.*.at", "datetime", default="time::now()")],
            [_f("meta", "object"), _f("meta.first-name", default="'x'", path=["meta", "first-name"])],
            [_f("meta", "object", default="{  }"), _f("meta.datetime", "int", default="1", path=["meta", "datetime"])],
            [_f("a.b", "int", default="1")],  # an undefined parent: dict[str, Any] | None
            [_f("prefs", "object"), _f("prefs.theme", default="'light'")],
            [_f("prefs", "object"), _f("prefs.at", "datetime", default="time::now()", default_always=True)],
            [_f("prefs", "object", default="time::now()"), _f("prefs.at", "datetime", default="time::now()")],
            [_f("prefs", "object", computed="a"), _f("prefs.at", "datetime", default="time::now()")],
            [_f("prefs", "object"), _f("prefs.r", "object", default="{  }"), _f("prefs.r.s", "int")],
            [_f("tags", "array<string>"), _f("tags.*", default="'x'")],
            [_f("prefs", "object"), _f("prefs.at", "datetime", value="time::now()")],
            [_f("at", "datetime", default="time::now()")],
        ],
        ids=[
            "child",
            "optional-child",
            "grandchild",
            "factory-parent",
            "item",
            "dict",
            "dict-factory",
            "no-parent",
            "held",
            "always",
            "server-field",
            "computed",
            "required",
            "item-definition",
            "value",
            "top-level",
        ],
    )
    def test_no_sub_field_default_note(self, fields: list[FieldState]) -> None:
        """#261: ``save()`` reads back the sub-fields a ``DEFAULT`` fills on create, so no class says otherwise (the
        comment of spec §6.4.7, M48, is gone)."""
        source = _generate(*fields)
        assert "update() after save()" not in source
        assert _class_block(source, "T").splitlines()[1].startswith("    model_config = ")

    def test_update_note_follows_the_edge_ends(self) -> None:
        source = _generate(
            _f("created", "set<datetime>", readonly=True),
            _f("prefs", "object"),
            _f("prefs.at", "datetime", default="time::now()"),
            name="e",
            table_type="relation",
        )
        assert _class_block(source, "E").splitlines()[1:4] == [
            "    # IN any OUT any",
            UPDATE_NOTE,
            '    model_config = SurrealConfigDict(table_name="e")',
        ]

    def test_update_note_on_an_edge_but_not_for_in_out(self) -> None:
        edge = _generate(_f("in", "set<datetime>", readonly=True), name="e", table_type="relation")
        assert "update()" not in edge
        edge = _generate(_f("at", "set<datetime>", readonly=True), name="e", table_type="relation")
        assert _class_block(edge, "E").splitlines()[2] == UPDATE_NOTE

    def test_comment_order(self) -> None:
        source = _generate(
            _f("at", "set<datetime>", readonly=True),
            name="r",
            table_type="relation",
            schema_mode="SCHEMALESS",
            comment="c",
            view_query="SELECT * FROM x",
            changefeed="1d",
            permissions={"select": "FULL"},
            indexes={"i": IndexState(name="i", fields=("at",))},
            events={"e": EventState("e", "r", "true", "(x)")},
        )
        assert _class_block(source, "R").splitlines()[1:11] == [
            "    # Comment: c",
            "    # IN any OUT any",
            "    # View: AS SELECT * FROM x",
            "    # Changefeed: 1d",
            "    # Permissions: FOR select FULL",
            '    # SCHEMALESS: fields not declared here are kept (extra="allow")',
            "    # Indexes: i",
            "    # Events: e",
            UPDATE_NOTE,
            '    model_config = SurrealConfigDict(table_name="r", extra="allow")',
        ]

    def test_model_without_fields(self) -> None:
        assert _class_block(_generate(), "T") == (
            'class T(BaseSurrealModel):\n    model_config = SurrealConfigDict(table_name="t")\n\n    id: str | None = None\n'
        )


#: Spec §6.4.7, form 2 — exactly as the spec prints it.
FORM_2 = """\
    model_config = SurrealConfigDict(
        table_name="isp_audit_trail",
        connection="analytics_replica",
        server_fields=["created_at", "updated_at", "first_seen"],
        extra="allow",
    )"""

#: Spec §6.4.7, form 3, for the eight long names of AC-11 (in field-line order).
FORM_3 = """\
    model_config = SurrealConfigDict(
        table_name="isp_audit_trail_long",
        connection="analytics_replica",
        server_fields=[
            "archived_timestamp_fourth",
            "created_timestamp_first",
            "deleted_timestamp_third",
            "exported_timestamp_seventh",
            "imported_timestamp_eighth",
            "restored_timestamp_fifth",
            "updated_timestamp_second",
            "verified_timestamp_sixth",
        ],
    )"""

_LONG_NAMES = (
    "created_timestamp_first",
    "updated_timestamp_second",
    "deleted_timestamp_third",
    "archived_timestamp_fourth",
    "restored_timestamp_fifth",
    "verified_timestamp_sixth",
    "exported_timestamp_seventh",
    "imported_timestamp_eighth",
)


def _server_field(column: str, kind: str = "datetime") -> FieldState:
    """A column the server fills (an expression ``DEFAULT``): it goes to ``server_fields``."""
    return _f(column, kind, default="time::now()" if kind == "datetime" else "rand::int()", path=[column])


def _form_2_state() -> SchemaState:
    # "~first_seen" sorts after "updated_at" and is named first_seen: the spec's printed order.
    fields = [_server_field("created_at"), _server_field("updated_at"), _server_field("~first_seen")]
    table = TableState(name="isp_audit_trail", schema_mode="SCHEMALESS", fields={f.name: f for f in fields})
    return SchemaState(tables={table.name: table}, connection="analytics_replica")


def _form_3_state() -> SchemaState:
    fields = [_server_field(name) for name in _LONG_NAMES]
    table = TableState(name="isp_audit_trail_long", fields={f.name: f for f in fields})
    return SchemaState(tables={table.name: table}, connection="analytics_replica")


class TestModelConfig:
    """Spec §6.4.7 (D20): the three ``model_config`` forms and their 128-column boundaries."""

    def test_form_1(self) -> None:
        assert _model_config(GOLDEN, "IspPerson") == (
            '    model_config = SurrealConfigDict(table_name="isp_person", connection="isp", server_fields=["created"])'
        )

    def test_keyword_order(self) -> None:
        source = _generate(_server_field("at"), name="t", connection="c", schema_mode="SCHEMALESS")
        assert _model_config(source, "T") == (
            '    model_config = SurrealConfigDict(table_name="t", connection="c", server_fields=["at"], extra="allow")'
        )

    def test_form_2(self) -> None:
        source = ModelCodeGenerator().generate(_form_2_state())
        assert _model_config(source, "IspAuditTrail") == FORM_2
        assert _line_of(source, "first_seen") == 'first_seen: datetime | None = Field(default=None, alias="~first_seen")'

    def test_form_3(self) -> None:
        assert _model_config(ModelCodeGenerator().generate(_form_3_state()), "IspAuditTrailLong") == FORM_3

    def test_one_line_boundary(self) -> None:
        at_128 = _model_config(_generate(name="t" * 77))
        assert at_128 == '    model_config = SurrealConfigDict(table_name="' + "t" * 77 + '")'
        assert len(at_128) == 128
        at_129 = _model_config(_generate(name="t" * 78))
        assert at_129 == '    model_config = SurrealConfigDict(\n        table_name="' + "t" * 78 + '",\n    )'

    def test_server_fields_line_boundary(self) -> None:
        at_128 = _model_config(_generate(_server_field("s" * 101, "int")), "T")
        assert at_128.splitlines()[2] == '        server_fields=["' + "s" * 101 + '"],'
        assert len(at_128.splitlines()[2]) == 128
        at_129 = _model_config(_generate(_server_field("s" * 102, "int")), "T")
        assert at_129.splitlines()[2:5] == ["        server_fields=[", '            "' + "s" * 102 + '",', "        ],"]

    def test_wide_characters_count_twice(self) -> None:
        assert _width("分析") == 4
        assert _width("ab") == 2
        assert _width("ＡＢ") == 4  # full-width
        assert _width("é") == 1
        one_line = _model_config(_generate(name="t" * 58, connection="分析"))
        assert one_line == '    model_config = SurrealConfigDict(table_name="' + "t" * 58 + '", connection="分析")'
        assert len(one_line) == 126 and _width(one_line) == 128
        wrapped = _model_config(_generate(name="t" * 59, connection="分析"))
        assert wrapped == (
            '    model_config = SurrealConfigDict(\n        table_name="' + "t" * 59 + '",\n        connection="分析",\n    )'
        )

    def test_a_single_long_keyword_line_stays(self) -> None:
        source = _generate(name="t" * 140)
        assert _model_config(source).splitlines()[1] == '        table_name="' + "t" * 140 + '",'

    def test_forms_pass_ruff(self, tmp_path: Path) -> None:
        _quality(tmp_path, _form_2_state(), "form2")
        _quality(tmp_path, _form_3_state(), "form3")
        for name, state in [
            ("at128", SchemaState(tables={"t" * 77: TableState(name="t" * 77)})),
            ("at129", SchemaState(tables={"t" * 78: TableState(name="t" * 78)})),
            ("wide128", SchemaState(tables={"t" * 58: TableState(name="t" * 58)}, connection="分析")),
            ("wide129", SchemaState(tables={"t" * 59: TableState(name="t" * 59)}, connection="分析")),
        ]:
            _quality(tmp_path, state, name)
        for length in (101, 102):
            field = _server_field("s" * length, "int")
            _quality(tmp_path, SchemaState(tables={"t": TableState(name="t", fields={field.name: field})}), f"sf{length}")


class TestModule:
    """Spec §6.4.8: docstring, imports, skip lines, blank lines."""

    def test_keyword_built_state(self, tmp_path: Path) -> None:
        state = SchemaState(tables={"t": TableState(name="t", fields={"x": FieldState(name="x", field_type="int")})})
        source = _quality(tmp_path, state, "keyword")
        assert source == (
            DOCSTRING
            + "\n\nfrom surreal_orm_lite import BaseSurrealModel, SurrealConfigDict\n\n\n"
            + "class T(BaseSurrealModel):\n"
            + '    model_config = SurrealConfigDict(table_name="t")\n\n'
            + "    id: str | None = None\n"
            + "    x: int | None = None\n"
        )
        any_state = SchemaState(tables={"t": TableState(name="t", fields={"y": FieldState(name="y", field_type="any")})})
        any_source = _quality(tmp_path, any_state, "keyword_any")
        assert _line_of(any_source, "y") == "y: Any = None"
        assert "from typing import Any\n" in any_source

    def test_empty_state(self, tmp_path: Path) -> None:
        source = _quality(tmp_path, SchemaState(), "empty")
        assert source == DOCSTRING + "\n"

    def test_only_skipped_tables(self, tmp_path: Path) -> None:
        state = SchemaState(tables={"select": TableState(name="select")})
        source = _quality(tmp_path, state, "skipped")
        assert source == (
            DOCSTRING
            + "\n\n# Not generated:\n"
            + "# - table 'select': its name is a SurrealQL keyword the ORM cannot use as a table name\n"
        )

    def test_skip_lines_s1_to_s6(self, tmp_path: Path) -> None:
        tables = {
            "isp-weird": TableState(name="isp-weird"),
            "select": TableState(name="select"),
            "b_unread": TableState(name="b_unread"),
            "d": TableState(
                name="d",
                fields={
                    "k": FieldState(name="k", field_type="frob<", nullable=False),
                    "dup": FieldState(name="dup", field_type="int"),
                    "p.q": FieldState(name="p.q", field_type="int"),
                },
            ),
        }
        issues = [
            SchemaIssue(kind="table", name="b_unread", table="b_unread", message="INFO FOR TABLE failed: boom"),
            SchemaIssue(
                kind="table",
                name="c_unparsed",
                table="c_unparsed",
                message="could not parse its definition: no reading of its clauses fits",
                statement="DEFINE TABLE c_unparsed …",
            ),
            SchemaIssue(kind="table", name="isp-weird", table="isp-weird", message="INFO FOR TABLE failed: boom"),  # S1 wins
            SchemaIssue(
                kind="field", name="`x.y`", table="d", message="could not parse its definition: its kind is not understood"
            ),
            SchemaIssue(kind="field", name="dup", table="d", message="two definitions have the same name"),
            SchemaIssue(kind="field", name="k", table="d", message="its TYPE clause was not understood and is kept as written"),
            SchemaIssue(kind="field", name="z", table="select", message="could not parse its definition: x"),  # table skipped
            SchemaIssue(kind="index", name="ix", table="d", message="could not parse its definition: x"),
            SchemaIssue(kind="section", name="apis", table=None, message="section 'apis' is not a mapping of names"),
        ]
        source = _quality(tmp_path, SchemaState(tables=tables, issues=issues), "skips")
        header = source.split("\n\n\n")[0]
        assert header.endswith(
            "from typing import Any\n\n"
            "from surreal_orm_lite import BaseSurrealModel, SurrealConfigDict\n\n"
            "# Not generated:\n"
            "# - table 'b_unread': it could not be read (see SchemaState.issues)\n"
            "# - table 'c_unparsed': its definition could not be parsed (see SchemaState.issues)\n"
            "# - field 'd.`x.y`': its definition could not be parsed (see SchemaState.issues)\n"
            "# - field 'd.dup': another definition has the same name (see SchemaState.issues)\n"
            "# - field 'd.p.q': its parent is not defined (the parent is a dict[str, Any])\n"
            "# - table 'isp-weird': its name is not an identifier, which table_name requires\n"
            "# - table 'select': its name is a SurrealQL keyword the ORM cannot use as a table name"
        )
        assert "class BUnread(" not in source
        assert _line_of(source, "k") == "k: Any = None"  # I3: generated, typed Any
        assert [line for line in source.splitlines() if line.startswith("class ")] == ["class D(BaseSurrealModel):"]

    def test_issue_texts_are_the_readers(self) -> None:
        assert model_generator_module._I3_TYPE_AS_WRITTEN == introspection_module._TYPE_AS_WRITTEN
        assert model_generator_module._I5_SAME_NAME == introspection_module._SAME_NAME

    def test_skip_line_names_are_escaped(self) -> None:
        source = ModelCodeGenerator().generate(SchemaState(tables={"a\nb é": TableState(name="a\nb é")}))
        # repr() first (the newline becomes a backslash and an n), then the comment's escaping (the backslash doubled).
        assert "# - table 'a\\\\nb \\xe9': its name is not an identifier, which table_name requires" in source

    def test_imports_minimal_ordered_and_wrapped(self) -> None:
        source = _generate(
            _f("a", "uuid"),
            _f("b", "geometry<point>", nullable=True),
            _f("c", "geometry<polygon>"),
            _f("d", "range"),
            _f("e", "geometry"),
            _f("f", "set<duration>"),
            _f("g", "geometry<multipoint>", nullable=True),
        )
        assert source.split("\n\n\nclass")[0] == (
            DOCSTRING
            + "\n\nfrom uuid import UUID\n\n"
            + "from surreal_orm_lite import (\n"
            + "    BaseSurrealModel,\n"
            + "    Duration,\n"
            + "    Geometry,\n"
            + "    GeometryPolygon,\n"
            + "    MultiPointField,\n"
            + "    PointField,\n"
            + "    Range,\n"
            + "    SurrealConfigDict,\n"
            + ")"
        )
        for line in source.splitlines():
            if line.startswith("from "):
                assert len(line) <= 88

    def test_layout(self) -> None:
        source = ModelCodeGenerator().generate(_fixture_state(3))
        assert source.endswith("\n") and not source.endswith("\n\n")
        assert "\n\n\n\n" not in source
        assert source.count("\n\n\nclass ") == 5


class TestImportable:
    """AC-11, AC-17, AC-18 (unit) and AC-9 (unit): generated modules import and behave."""

    def test_quality_state_ac11(self, tmp_path: Path) -> None:
        defaults = {field.name: field for field in (parse_define_field(s).state for s, _ in LITERAL_DEFAULTS)}
        tables = {
            "t": TableState(name="t", fields=defaults),
            "kinds": TableState(
                name="kinds",
                comment="c" * 300,
                fields={
                    **{
                        f"x{index:02}": _f(f"x{index:02}", kind, nullable=nullable)
                        for index, (kind, nullable, _) in enumerate(TYPE_ROWS)
                    },
                    "l": _f("l", "int"),
                    "O": _f("O", "int"),
                    "I": _f("I", "int"),
                },
            ),
            "i": TableState(name="i", fields={"l": _f("l", "int")}),
            "o": TableState(name="o", table_type="relation"),
        }
        source = _quality(tmp_path, SchemaState(tables=tables, connection="isp"), "ac11")
        assert "class IModel(BaseSurrealModel):" in source
        assert "class OModel(BaseSurrealEdge):" in source
        for name in ("l", "O", "I"):
            assert _line_of(_class_block(source, "Kinds"), name + "_") == f'{name}_: int = Field(alias="{name}")'
        comment = _class_block(source, "Kinds").splitlines()[1]
        assert len(comment) == 128 and comment.endswith("...")

    def test_reserved_columns_ac17(self, tmp_path: Path) -> None:
        reserved = ["datetime", "computed", "list", "str", "Field", "Any", "l", "O", "I", "IspPersonAddress"]
        fields = [
            *(_f(column, "string", nullable=True) for column in reserved),
            _f("address", "object"),
            _f("address.city"),
            _f("geo", "object", nullable=True),
            _f("geo.datetime", "datetime"),
            _f("updated", "datetime"),
            _f("x", "int", computed="a * 2"),
            _f("tags", "array<string>"),
            _f("name"),
        ]
        state = SchemaState(tables={"isp_person": TableState(name="isp_person", fields={f.name: f for f in fields})})
        source = ModelCodeGenerator().generate(state)
        module = _import(tmp_path, source)
        model = module.IspPerson
        for column in reserved:
            assert model.model_fields[column + "_"].alias == column, column
        assert model.model_fields["updated"].annotation is datetime
        assert model.model_fields["name"].annotation is str
        assert model.model_fields["tags"].annotation == list[str]
        assert model.get_computed_fields() == {"x": "a * 2"}
        assert _line_of(source, "geo") == "geo: dict[str, Any] | None = None"
        assert not hasattr(module, "IspPersonGeo")
        assert module.IspPersonAddress.model_fields["city"].annotation is str
        _ruff(tmp_path, source, "ac17")

    def test_server_fields_by_attribute_ac18(self, tmp_path: Path) -> None:
        fields = [
            parse_define_field("DEFINE FIELD `created-at` ON isp_sf TYPE datetime DEFAULT time::now() PERMISSIONS FULL").state,
            parse_define_field("DEFINE FIELD x ON isp_sf TYPE string VALUE string::concat(a, ';', b) PERMISSIONS FULL").state,
        ]
        state = SchemaState(tables={"isp_sf": TableState(name="isp_sf", fields={f.name: f for f in fields})})
        module = _import(tmp_path, _quality(tmp_path, state, "ac18"))
        assert module.IspSf.get_server_fields() >= {"created_at", "x"}
        assert module.IspSf.model_fields["created_at"].alias == "created-at"

    @pytest.mark.parametrize("rendering", [0, 1], ids=["2x", "3x"])
    def test_constant_defaults_ac18b(self, tmp_path: Path, rendering: int) -> None:
        """AC-18b (unit): the §9.2 rows d5, d6, d11, d15, d25–d31, p1 and p2 give the §6.4.6 lines; the module passes
        ruff and imports; ``get_server_fields()`` holds d20, d22, d28 and d31 only; ``p2`` is required; the factories
        give each instance its own value."""
        wanted = {"d5", "d6", "d11", "d15", "d20", "d22", *(f"d{n}" for n in range(25, 32))}
        rows = [(statement, line) for statement, line in LITERAL_DEFAULTS if statement.split()[2] in wanted]
        d20 = "math::INF" if rendering == 0 else "math::INFINITY"
        rows = [(s, line) for s, line in rows if not s.startswith("DEFINE FIELD d20") or d20 + " " in s]
        fields = [parse_define_field(statement).state for statement, _ in rows]
        fields += [parse_define_field(statement).state for statement in SUB_MODEL_DEFAULTS[rendering]]
        state = SchemaState(tables={"t": TableState(name="t", fields={field.name: field for field in fields})})
        source = _quality(tmp_path, state, f"ac18b_{rendering}")
        for statement, line in rows:
            assert _line_of(_class_block(source, "T"), statement.split()[2]) == line
        assert _class_block(source, "TP1") == 'class TP1(BaseModel):\n    theme: str = "light"\n'
        assert _line_of(_class_block(source, "T"), "p1") == "p1: TP1 = Field(default_factory=TP1)"
        assert _line_of(_class_block(source, "T"), "p2") == "p2: TP2 | None"
        module = _import(tmp_path, source)
        model = module.T
        assert model.get_server_fields() == frozenset({"d20", "d22", "d28", "d31"})
        assert model.model_fields["p2"].is_required()
        first, second = model(p2=None), model(p2=None)
        assert (first.d5, first.d15) == (datetime.fromisoformat("2020-01-01T00:00:00Z"), uuid.UUID(_UUID_TEXT))
        assert (first.d6, first.d29) == (Duration.parse("1h30m"), Duration.parse("1h30m5ns"))
        assert (first.d11, first.d25, first.d26, first.d27, first.d30) == ([], [1, 2], set(), {}, [[1], []])
        assert first.p1 == module.TP1(theme="light")
        for name in ("d6", "d11", "d25", "d26", "d27", "d29", "d30", "p1"):
            assert getattr(first, name) is not getattr(second, name), name

    def test_if_renderings_ac9(self) -> None:
        sources = [
            _generate(parse_define_field(statement).state)
            for statement in (
                "DEFINE FIELD i1 ON t VALUE IF b THEN 1 ELSE 2 END PERMISSIONS FULL",
                "DEFINE FIELD i1 ON t VALUE (IF b THEN 1 ELSE 2 END) PERMISSIONS FULL",
            )
        ]
        two, three = (source.splitlines() for source in sources)
        different = [(a, b) for a, b in zip(two, three, strict=True) if a != b]
        assert different == [
            (
                '    i1: Computed[Any] = computed("IF b THEN 1 ELSE 2 END")',
                '    i1: Computed[Any] = computed("(IF b THEN 1 ELSE 2 END)")',
            )
        ]

    def test_generate_is_pure(self) -> None:
        state = _fixture_state(3)
        generator = ModelCodeGenerator()
        assert generator.generate(state) == generator.generate(state) == ModelCodeGenerator().generate(_fixture_state(3))
        assert inspect.iscoroutinefunction(ModelCodeGenerator.generate) is False
        assert list(inspect.signature(ModelCodeGenerator.generate).parameters) == ["self", "state"]
        assert list(inspect.signature(ModelCodeGenerator).parameters) == []


def _surql_string(text: str) -> str:
    """``text`` as a single-quoted SurrealQL string literal (what ``unquote()`` reads back)."""
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _corpus_state() -> SchemaState:
    """Spec AC-12: the §8 corpus at every position server text can reach the generated source."""
    tables: dict[str, TableState] = {}
    for index, text in enumerate(CORPUS):
        tables[text] = TableState(name=text)  # a non-identifier table: skipped (S1), named in a comment only
        name = f"inj_{index}"
        fields = [
            FieldState(name=text, field_type="string", path=[text]),
            FieldState(name="v", field_type="string", nullable=False, value=text),
            FieldState(name="k", field_type="any", computed=text),
            FieldState(name="d", field_type="string", nullable=False, default=text),
            FieldState(name="lit", field_type=_surql_string(text) + " | 'b'", nullable=False),
            FieldState(name="dl", field_type="string", nullable=False, default=_surql_string(text)),
            FieldState(name="sub", field_type="object", nullable=True),
            FieldState(name="sub.x", field_type="string", default=text),
            # D25 amended: constant defaults whose parts are server text (array elements, durations, datetimes, UUIDs).
            FieldState(name="arr", field_type="array<string>", nullable=False, default="[" + _surql_string(text) + ", 'b']"),
            FieldState(name="raw_items", field_type="array", nullable=False, default="[" + text + "]"),
            FieldState(name="dur", field_type="duration", nullable=False, default=text),
            FieldState(name="when", field_type="datetime", nullable=False, default="d'" + text + "'"),
            FieldState(name="uid", field_type="uuid", nullable=False, default="u'" + text + "'"),
        ]
        tables[name] = TableState(
            name=name,
            table_type="relation" if index % 2 else "normal",
            schema_mode="SCHEMALESS" if index % 3 else "SCHEMAFULL",
            comment=text,
            view_query=text,
            changefeed=text,
            permissions={"select": "WHERE " + text, "create": text, text: "FULL"},
            relation_in=(text,),
            relation_out=(text, "b"),
            fields={field.name: field for field in fields},
            indexes={text: IndexState(name=text, fields=(text,), unique=True)},
            events={text: EventState(text, name, text, text)},
        )
    issues = [
        SchemaIssue(kind="field", name=text, table="inj_0", message="could not parse its definition: x") for text in CORPUS
    ]
    return SchemaState(tables=tables, connection="__import__('os').system('x')", issues=issues)


def _guarded_exec(source: str) -> dict[str, Any]:
    """``exec`` the module in a fresh namespace whose ``__import__`` refuses ``os``/``subprocess``/``shutil``."""
    real_import = builtins.__import__

    def guarded(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.partition(".")[0] in {"os", "subprocess", "shutil", "socket", "sys", "builtins", "importlib"}:
            raise AssertionError(f"the generated module imported {name!r}")
        return real_import(name, *args, **kwargs)

    sandbox_builtins = dict(vars(builtins))
    sandbox_builtins["__import__"] = guarded
    namespace: dict[str, Any] = {"__builtins__": sandbox_builtins, "__name__": "generated_guarded"}
    exec(compile(source, "<generated>", "exec"), namespace)
    return namespace


def _code_text(source: str) -> str:
    """The module's code tokens (names, operators, numbers) joined by single spaces: no string, no comment."""
    kept = (tokenize.NAME, tokenize.OP, tokenize.NUMBER)
    return " ".join(token.string for token in tokenize.generate_tokens(io.StringIO(source).readline) if token.type in kept)


class TestInjection:
    """AC-12: server text reaches the source only as string literals and comments; the module parses and imports."""

    def test_corpus_everywhere(self, tmp_path: Path) -> None:
        state = _corpus_state()
        source = ModelCodeGenerator().generate(state)
        assert source == ModelCodeGenerator().generate(state)
        tree = ast.parse(source)
        first, *rest = tree.body
        assert isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
        kinds = [type(node) for node in rest]
        assert kinds == sorted(kinds, key=lambda kind: kind is ast.ClassDef)
        assert set(kinds) == {ast.ImportFrom, ast.ClassDef}
        for node in rest:
            if isinstance(node, ast.ClassDef):
                assert all(isinstance(item, ast.AnnAssign | ast.Assign) for item in node.body), node.name
        namespace = _guarded_exec(source)
        defined = {
            key for key, value in namespace.items() if isinstance(value, type) and value.__module__ == "generated_guarded"
        }
        expected = {f"Inj{index}" for index in range(len(CORPUS))} | {f"Inj{index}Sub" for index in range(len(CORPUS))}
        assert defined == expected
        _import(tmp_path, source)
        code = _code_text(source)
        for text in CORPUS:
            if len(text.strip()) >= 3:
                assert text not in code, text
        # The payloads' names never stand as names of their own (an allocated attribute may spell them inside it).
        assert not set(code.split()) & {"__import__", "os", "system", "exec", "eval", "subprocess", "REMOVE"}
        _comment_lines_fit(source)

    def test_corpus_values_round_trip(self, tmp_path: Path) -> None:
        module = _import(tmp_path, ModelCodeGenerator().generate(_corpus_state()))
        for index, text in enumerate(CORPUS):
            model = getattr(module, f"Inj{index}")
            aliases = {field.alias for field in model.model_fields.values()}
            assert text in aliases or model.model_fields.get(text) is not None, text
            assert model.model_fields["dl"].default == text
            factory = model.model_fields["arr"].default_factory
            assert factory is not None and factory() == [text, "b"], text
            assert model.model_config["table_name"] == f"inj_{index}"
            assert model.model_config["connection"] == "__import__('os').system('x')"
            literal = model.model_fields["lit"].annotation
            assert set(literal.__args__) == ({text, "b"}), text

    def test_random_strings(self) -> None:
        for text in RANDOM:
            assert ast.literal_eval(_py_str(text)) == text
            assert "\n" not in _comment(text, indent=0)


def _random_state(rng: random.Random) -> SchemaState:
    """A schema mixing kinds, names, flags, defaults, expressions, nested paths and table shapes at random."""
    kinds = [kind for kind, _, _ in TYPE_ROWS] + ["object", "array<object>", "option<object>", "set<object>"]
    columns = ["a", "b", "class", "first-name", "datetime", "l", "save", "model_x", "_p", "9x", "id", "in", "out", "é", "Tbl0"]
    columns += [text for text in CORPUS if text][:12]
    defaults = [None, "0", "1.5f", "1.5dec", "'x'", "true", "NONE", "NULL", "time::now()", "[]", "[1, NONE]", "{  }"]
    defaults += ["[[1], []]", "['a', \"b'c\"]", "1h30m", "d'2030-01-01T00:00:00Z'", "u'0190d3b4-0000-7000-8000-000000000000'"]
    expressions = [None, "time::now()", "string::concat(a, ';', b)", "$value * 2", "$input", "IF a THEN 1 ELSE 2 END", "x"]
    tables: dict[str, TableState] = {}
    for index in range(rng.randint(1, 4)):
        name = rng.choice([f"tbl{index}", f"Tbl{index}", "select", "a-b", "i", "Range"]) + ("" if index == 0 else str(index))
        fields: dict[str, FieldState] = {}
        for _ in range(rng.randint(0, 10)):
            path = [rng.choice(columns)]
            while rng.random() < 0.45:
                path.append(rng.choice(["*", *columns[:6]]))
            expression = rng.choice(expressions)
            field = FieldState(
                name=".".join(path),
                field_type=rng.choice(kinds),
                nullable=rng.random() < 0.5,
                default=rng.choice(defaults),
                readonly=rng.random() < 0.2,
                flexible=rng.random() < 0.2,
                value=expression if rng.random() < 0.5 else None,
                computed=expression if rng.random() < 0.2 else None,
                path=path,
            )
            fields[field.name] = field
            for depth in range(1, len(path)):  # most nested fields get their parents defined: sub-models
                parent = path[:depth]
                if ".".join(parent) not in fields and rng.random() < 0.8:
                    kind = "array<object>" if path[depth] == "*" else rng.choice(["object", "option<object>"])
                    fields[".".join(parent)] = FieldState(
                        name=".".join(parent), field_type=kind, path=parent, flexible=rng.random() < 0.3
                    )
        tables[name] = TableState(
            name=name,
            schema_mode=rng.choice(["SCHEMAFULL", "SCHEMALESS"]),
            table_type=rng.choice(["normal", "any", "relation", "RELATION"]),
            fields=fields,
            comment=rng.choice([None, "c", "\n#"]),
            changefeed=rng.choice([None, "1d", "\n#"]),
            permissions=rng.choice([{}, {"select": "WHERE \n#"}, dict.fromkeys(("select", "create"), "FULL")]),
            relation_in=rng.choice([(), ("a",)]),
            enforced=rng.random() < 0.5,
        )
    issues = [SchemaIssue(kind="field", name="z", table=name, message="could not parse its definition: x") for name in tables]
    return SchemaState(tables=tables, connection=rng.choice(["default", "isp", "分析"]), issues=issues)


class TestRandomStates:
    """Seeded random schemas: whatever the mix, the module compiles, imports and is deterministic."""

    def test_seeded_random_states_import(self, tmp_path: Path) -> None:
        rng = random.Random(30_008)
        for _ in range(40):
            state = _random_state(rng)
            source = ModelCodeGenerator().generate(state)
            assert source == ModelCodeGenerator().generate(state)
            tree = ast.parse(source)
            for node in tree.body[1:]:
                assert isinstance(node, ast.ImportFrom | ast.ClassDef)
            _comment_lines_fit(source)
            _import(tmp_path, source)


class TestExports:
    """AC-1 (part): ``ModelCodeGenerator`` is exported by the package and by ``introspection``."""

    def test_exported(self) -> None:
        assert "ModelCodeGenerator" in surreal_orm_lite.__all__
        assert "ModelCodeGenerator" in introspection_module.__all__
        assert surreal_orm_lite.ModelCodeGenerator is introspection_module.ModelCodeGenerator
        assert surreal_orm_lite.ModelCodeGenerator is model_generator_module.ModelCodeGenerator
