r"""Generate ``BaseSurrealModel`` source from a ``SchemaState`` (v0.30.0) — private.

``ModelCodeGenerator`` (re-exported by :mod:`surreal_orm_lite.introspection`) is implemented here. The schema it
reads came from the server: table and field names, comments and expressions are server text, and the generated
module is source code someone will import. So server text reaches that source in exactly three ways, and through no
format placeholder of the generator's own:

- ``_py_str()`` — a Python string literal: ``ast.literal_eval(_py_str(s)) == s`` for every ``s``, quoted the way
  ruff format quotes it, with every character that is not printable escaped (``\n``, ``\x0c``, `` `` …);
- ``_comment()`` — one comment line of printable ASCII (``unicode_escape``), no trailing whitespace, cut with
  ``...`` to fit 128 columns; every comment the generator writes starts with a fixed prefix of its own (``Comment:``,
  ``- table`` …), so server text never begins one (no ``# type:``, ``# noqa`` or ``# fmt:`` directive);
- the names of ``_Names`` — class and attribute names built from ``[A-Za-z0-9_]`` only, never a reserved word, an
  imported name, a builtin used in annotations or a name ruff calls ambiguous; a column whose name cannot be used as it
  is gets an attribute name of its own and ``Field(alias=<column>)``.

**Tables.** A table is modelled when its name is an identifier (``table_name`` refuses anything else, S1) and is not
one of the SurrealQL words the ORM's unescaped statements cannot use as a table name, whatever the case (S6, M37:
``select``, ``Select``, ``only`` …); the others are listed in the module's header instead.

**Names are allocated in two passes**, so that no attribute can shadow a class of the module: first the class names —
tables in codepoint order of their names, each followed by its sub-model candidates (an ``object`` field with
declared children, recursively; an ``array<object>``'s items) — then each table's attribute names, columns in
codepoint order. The same schema always gives the same names.
"""

import keyword
import re
from collections.abc import Iterable, Mapping, Sequence

from pydantic import BaseModel

from .define_parser import DefineParseError, Kind, KName, parse_kind
from .edge import BaseSurrealEdge
from .model_base import BaseSurrealModel
from .schema_state import FieldState, TableState
from .utils import VALID_ALIAS_PATTERN

#: Every name the generated module may import (spec §6.4.4 and §6.4.8), used or not, so that a table or a column
#: never takes one of them — and adding a type to a schema never renames anything.
_IMPORTABLE = frozenset(
    {
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
    }
)

#: Builtins the annotations use. The generated module has no ``from __future__ import annotations``: a class
#: attribute named ``list`` would break the next ``list[str]`` of the class body, one named ``str`` would turn the next
#: ``str`` annotations into ``None`` silently.
_ANNOTATION_BUILTINS = frozenset({"str", "int", "float", "bool", "bytes", "list", "set", "dict", "tuple"})

#: Names ruff reports as ambiguous (E741 for an attribute, E742 for a class — M41).
_AMBIGUOUS = frozenset({"l", "O", "I"})

#: Python keywords and soft keywords. The soft keywords are fixed — those of Python 3.11 to 3.15 — so the generated
#: names do not depend on the Python that runs the generator (3.11 has no ``type``, 3.15 adds ``lazy``).
_KEYWORDS = frozenset(keyword.kwlist) | frozenset(keyword.softkwlist) | frozenset({"_", "case", "lazy", "match", "type"})

#: SurrealQL words the ORM's statements cannot use as a table name (they interpolate it unescaped:
#: ``SELECT * FROM <table>``, ``CREATE <table> …``), compared lower-cased (M37, spec §6.4.2; §14.7 tracks the ORM).
_UNQUERYABLE_TABLE_NAMES = frozenset(
    {
        "alter",
        "break",
        "continue",
        "create",
        "define",
        "delete",
        "explain",
        "false",
        "for",
        "function",
        "if",
        "info",
        "insert",
        "let",
        "none",
        "null",
        "only",
        "outside",
        "rebuild",
        "relate",
        "remove",
        "return",
        "select",
        "sleep",
        "throw",
        "true",
        "update",
        "upsert",
    }
)

#: Why a table is not generated (spec §6.4.8: the header's skip lines S1 and S6).
_S1_NOT_IDENTIFIER = "its name is not an identifier, which table_name requires"
_S6_KEYWORD = "its name is a SurrealQL keyword the ORM cannot use as a table name"

#: The widest line a comment of the generated module may make (ruff's E501 at ``--line-length 128``).
_MAX_WIDTH = 128

#: What a model or an edge class already provides: ``id`` is declared first by every model, and ``BaseSurrealEdge``
#: declares ``id``, ``in_`` and ``out``.
_MODEL_PROVIDED = frozenset({"id"})
_EDGE_PROVIDED = frozenset({"id", "in_", "out"})

_NOT_ALPHANUMERIC = re.compile(r"[^A-Za-z0-9]+")
_NOT_WORD = re.compile(r"[^A-Za-z0-9_]")


def _py_str(text: str) -> str:
    """Return a Python string literal of ``text``: ``ast.literal_eval(_py_str(text)) == text``.

    The quote is ruff format's choice: ``'`` when ``text`` holds more ``"`` than ``'``, else ``"``. Escaped:
    the backslash, the chosen quote, ``\\n``, ``\\r``, ``\\t``, and every other character that is not printable
    (``str.isprintable()``) as ``\\xhh``, ``\\uhhhh`` or ``\\Uhhhhhhhh`` — so the literal holds no line break, no
    control character and no invisible separator, whatever the server sent.
    """
    quote = "'" if text.count('"') > text.count("'") else '"'
    out = [quote]
    for char in text:
        if char == "\\" or char == quote:
            out.append("\\" + char)
        elif char == "\n":
            out.append("\\n")
        elif char == "\r":
            out.append("\\r")
        elif char == "\t":
            out.append("\\t")
        elif char.isprintable():
            out.append(char)
        else:
            code = ord(char)
            if code < 0x100:
                out.append(f"\\x{code:02x}")
            elif code < 0x10000:
                out.append(f"\\u{code:04x}")
            else:
                out.append(f"\\U{code:08x}")
    out.append(quote)
    return "".join(out)


def _comment(text: str, *, indent: int) -> str:
    """Return ``text`` as one comment line (``"# …"``, without its indentation) for a line indented by ``indent``.

    The text is escaped with ``unicode_escape`` — printable ASCII only, so no line break, form feed, separator or
    non-ASCII character reaches the source — and stripped of trailing whitespace (W291). A line that would pass 128
    columns is cut and ends with ``...`` (E501). The caller puts a fixed prefix of its own first (``"Comment: "`` …).
    """
    line = ("# " + text.encode("unicode_escape").decode("ascii")).rstrip()
    if indent + len(line) > _MAX_WIDTH:
        line = line[: _MAX_WIDTH - 3 - indent].rstrip() + "..."
    return line


def _skip_reason(table: str) -> str | None:
    """Return why the table named ``table`` is not generated (S1, S6), or ``None`` when it is modelled."""
    if not VALID_ALIAS_PATTERN.fullmatch(table):
        return _S1_NOT_IDENTIFIER
    if table.lower() in _UNQUERYABLE_TABLE_NAMES:
        return _S6_KEYWORD
    return None


def _modelable(table: str) -> bool:
    """Whether a model can be generated for the table named ``table`` (spec §6.4.2)."""
    return _skip_reason(table) is None


def _pascal(text: str) -> str:
    """``text`` split on ``_`` and every other non-ASCII-alphanumeric character, each part's first letter capitalised.

    ``game_tables`` → ``GameTables``, ``Person`` → ``Person``, ``first-name`` → ``FirstName``; ``""`` when nothing
    is left (``__``).
    """
    return "".join(part[0].upper() + part[1:] for part in _NOT_ALPHANUMERIC.split(text) if part)


def _base_attribute(name: str) -> bool:
    """Whether ``name`` is an attribute of the generated classes' bases (``save``, ``objects``, ``json``, ``schema`` …)."""
    return hasattr(BaseSurrealModel, name) or hasattr(BaseSurrealEdge, name) or hasattr(BaseModel, name)


def _kind(field: FieldState) -> Kind | None:
    """The parsed kind of ``field``, or ``None`` when its ``TYPE`` was kept as written (I3)."""
    try:
        return parse_kind(field.field_type)
    except DefineParseError:
        return None


def _is_object(kind: Kind | None) -> bool:
    return isinstance(kind, KName) and kind.name == "object"


def _array_item(kind: KName) -> Kind | None:
    """The item kind of an ``array<T[, N]>`` kind; ``None`` for a bare ``array``."""
    if kind.args and not isinstance(kind.args[0], str):
        return kind.args[0]
    return None


class _Names:
    """The class and attribute names of one generated module (spec §6.4.2), allocated in two passes.

    1. **Class names**, tables in codepoint order of their names, each followed by its sub-model candidates. The
       class of a table is its name in PascalCase (``isp_company`` → ``IspCompany``; ``Table`` when nothing is left,
       ``T`` before a leading digit; no singularisation); a sub-model candidate's is ``<Class><PascalColumn>``
       (``IspPersonAddress``, nested: ``IspPersonAddressGeo``), an ``array<object>``'s items ``<Class><PascalColumn>Item``.
       A name that is a keyword, an importable name, an ambiguous one or already allocated gets ``Model``, then
       ``Model2``, ``Model3`` …. A candidate that the generator later writes as ``dict[str, Any]`` keeps its name.
    2. **Attribute names** per table, columns in codepoint order: the column itself when it is an ASCII identifier
       that is not a keyword, does not start with ``_`` or ``model_``, is not an attribute of the bases
       (``save``, ``objects``, ``json`` …), not an importable name, an annotation builtin, an ambiguous name, a class
       name of the module, nor a name the class already provides (``id``; on an edge ``in_`` and ``out``) — else a
       name built from it (non-word characters → ``_``, leading ``_`` stripped, ``field`` when empty, ``f_`` before a
       leading digit, ``_`` after a ``model_`` prefix, then ``_`` while reserved: ``class`` → ``class_``) with the
       column as alias; ``_2``, ``_3`` … when the name is taken. ``id`` (and ``in``/``out`` on an edge) is never
       allocated: the class provides it.

    Only modelable tables (:func:`_modelable`) are named. A sub-model's own fields are its columns, unchanged (the ORM
    writes nested models by field name): :meth:`usable_child` says whether a column can be one.
    """

    def __init__(self, tables: Iterable[TableState]) -> None:
        modelled = sorted((table for table in tables if _modelable(table.name)), key=lambda table: table.name)
        self._taken: set[str] = set()
        self._classes: dict[str, str] = {}
        self._sub_models: dict[tuple[str, tuple[str, ...]], str] = {}
        for table in modelled:
            class_name = self._allocate_class(_table_class(table.name))
            self._classes[table.name] = class_name
            nodes = {tuple(field.path): field for field in table.fields.values()}
            for column in _top_level_columns(table):
                self._visit(table.name, nodes, (column,), class_name)
        self._fields = {table.name: self._allocate_fields(table) for table in modelled}

    @property
    def classes(self) -> Mapping[str, str]:
        """Table name → class name, in allocation order (codepoint order of the table names)."""
        return self._classes

    @property
    def sub_models(self) -> Mapping[tuple[str, tuple[str, ...]], str]:
        """``(table name, object path)`` → sub-model class name, in allocation order.

        The path is a field's ``path`` (``("address",)``, ``("address", "geo")``); for an array's items, the array's
        path and ``"*"`` (``("w", "*")``).
        """
        return self._sub_models

    @property
    def class_names(self) -> frozenset[str]:
        """Every class name of the module: tables and sub-model candidates."""
        return frozenset(self._taken)

    def class_name(self, table: str) -> str:
        """The class name of the table named ``table``."""
        return self._classes[table]

    def sub_model_class(self, table: str, path: Sequence[str]) -> str:
        """The class name of the sub-model candidate at ``path`` of the table named ``table``."""
        return self._sub_models[(table, tuple(path))]

    def fields(self, table: str) -> Mapping[str, tuple[str, str | None]]:
        """Column → ``(attribute name, alias)`` of the table named ``table``; the alias is ``None`` when unneeded."""
        return self._fields[table]

    def usable_child(self, column: str) -> bool:
        """Whether ``column`` can be a sub-model field as it is (else the parent is written ``dict[str, Any]``)."""
        return self._plain(column, frozenset())

    def _allocate_class(self, candidate: str) -> str:
        name = candidate
        count = 1
        while name in _KEYWORDS or name in _IMPORTABLE or name in _AMBIGUOUS or name in self._taken:
            count += 1
            name = candidate + ("Model" if count == 2 else f"Model{count - 1}")
        self._taken.add(name)
        return name

    def _visit(self, table: str, nodes: Mapping[tuple[str, ...], FieldState], path: tuple[str, ...], owner: str) -> None:
        """Allocate the sub-model candidates at and under ``path``, whose enclosing class is ``owner``."""
        node = nodes.get(path)
        kind = _kind(node) if node is not None else None
        column = _pascal(path[-1]) or "Field"
        if _is_object(kind):
            children = _children(nodes, path)
            if children:
                name = self._allocate_class(owner + column)
                self._sub_models[(table, path)] = name
                for child in children:
                    self._visit(table, nodes, (*path, child), name)
            return
        if not (isinstance(kind, KName) and kind.name == "array"):
            return
        item = (*path, "*")
        item_node = nodes.get(item)
        if _is_object(_kind(item_node) if item_node is not None else _array_item(kind)):
            children = _children(nodes, item)
            if children:
                name = self._allocate_class(owner + column + "Item")
                self._sub_models[(table, item)] = name
                for child in children:
                    self._visit(table, nodes, (*item, child), name)

    def _reserved(self, name: str, provided: frozenset[str]) -> bool:
        return (
            name in _KEYWORDS
            or name in _IMPORTABLE
            or name in _ANNOTATION_BUILTINS
            or name in _AMBIGUOUS
            or name in self._taken
            or name in provided
            or _base_attribute(name)
        )

    def _plain(self, column: str, provided: frozenset[str]) -> bool:
        return (
            column.isascii()
            and column.isidentifier()
            and not column.startswith(("_", "model_"))
            and not self._reserved(column, provided)
        )

    def _built(self, column: str, provided: frozenset[str]) -> str:
        name = _NOT_WORD.sub("_", column).lstrip("_") or "field"
        if name[0].isdigit():
            name = "f_" + name
        if name.startswith("model_"):
            name += "_"  # pydantic's own attributes never end with "_"
        while self._reserved(name, provided):
            name += "_"
        return name

    def _allocate_fields(self, table: TableState) -> dict[str, tuple[str, str | None]]:
        provided = _EDGE_PROVIDED if table.table_type == "relation" else _MODEL_PROVIDED
        allocated: dict[str, tuple[str, str | None]] = {}
        used: set[str] = set()
        for column in _top_level_columns(table):
            if self._plain(column, provided) and column not in used:
                attribute = column
            else:
                base = self._built(column, provided)
                attribute = base
                count = 1
                while attribute in used or self._reserved(attribute, provided):
                    count += 1
                    attribute = f"{base}_{count}"
            used.add(attribute)
            allocated[column] = (attribute, None if attribute == column else column)
        return allocated


def _table_class(table: str) -> str:
    """The class-name candidate of the table named ``table`` (before reservation)."""
    name = _pascal(table) or "Table"
    return "T" + name if name[0].isdigit() else name


def _top_level_columns(table: TableState) -> list[str]:
    """The table's top-level columns in codepoint order — of every field path, so an undefined parent of ``a.b`` too.

    ``id`` is left out (every class provides it), and ``in`` and ``out`` on an edge (``BaseSurrealEdge`` provides them).
    """
    skipped = {"id", "in", "out"} if table.table_type == "relation" else {"id"}
    return sorted({field.path[0] for field in table.fields.values() if field.path} - skipped)


def _children(nodes: Mapping[tuple[str, ...], FieldState], path: tuple[str, ...]) -> list[str]:
    """The declared direct children of ``path`` (not its items), in codepoint order."""
    depth = len(path) + 1
    return sorted(child[-1] for child in nodes if len(child) == depth and child[: len(path)] == path and child[-1] != "*")
