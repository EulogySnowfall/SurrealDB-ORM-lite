r"""Generate ``BaseSurrealModel`` source from a ``SchemaState`` (v0.30.0) — private.

``ModelCodeGenerator`` (re-exported by :mod:`surreal_orm_lite.introspection`) is implemented here:
``ModelCodeGenerator().generate(state)`` returns the source of one module — a ``BaseSurrealModel`` class per table
(``BaseSurrealEdge`` for a relation table), with ``model_config = SurrealConfigDict(table_name=…)``, the connection's
name when it is not ``"default"``, ``server_fields`` and ``extra="allow"`` for a ``SCHEMALESS`` table. Pure and
deterministic: the same state always gives the same string. The schema it
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

**Fields** (spec §6.4.3–§6.4.6). ``id`` is declared first by every model (an edge's ``id``, ``in_`` and ``out`` come
from ``BaseSurrealEdge``); each top-level field becomes one line, in codepoint order of its column. Kinds map to the
ORM's types (``Duration``, ``Decimal``, ``UUID``, ``RecordID``, ``Range``, the geometry classes, ``Literal``,
``set[T]`` for hashable ``T`` …); an optional kind adds ``| None = None``. An ``object`` with declared children becomes
a ``BaseModel`` sub-model written before its class; other objects are ``dict[str, Any]``. ``COMPUTED e``, and a
``VALUE e`` that reads neither ``$value`` nor ``$input``, become ``Computed[T] = computed("e")`` when ``computed()``
accepts ``e`` and the column needs no alias. A constant ``DEFAULT`` the model holds exactly becomes its Python default
(spec §6.4.6 item 3, D25, M48): the literals that match the annotation, ``[]`` and arrays of such constants, ``{  }``
(a ``dict``, or the sub-model when it can be built without arguments — else the field is required), durations,
datetimes with at most 6 fractional digits and UUIDs; mutable values (``list``, ``set``, ``dict``, a sub-model,
``Duration``) through ``default_factory``. So the server stores the same row whether the caller sets the field or not.
Every other ``DEFAULT`` or computed expression gives the **server form** — ``T | None = None``, the attribute listed
in ``server_fields`` so the server's value applies on create and is read back. ``ASSERT``, ``PERMISSIONS``,
``REFERENCE`` and field comments are not generated; ``READONLY`` is a trailing comment, and a class with a ``READONLY``
field holding a datetime below the top level (in a sub-object, a tuple, an array or a set), or with more than 32
top-level fields that may hold a datetime, says that ``update()`` may fail on it (§6.4.7, narrowed by #261).

**Module** (spec §6.4.8): a fixed docstring, the imports the classes use (standard library, then pydantic,
surreal_orm_lite and surrealdb), a ``# Not generated:`` comment listing every table or field left out and why, then the
classes. No timestamp, server address, namespace or database name. No ``from __future__ import annotations``: the
annotations are evaluated, which is why no attribute may shadow a name they use.
"""

import io
import keyword
import math
import re
import tokenize
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import NamedTuple
from uuid import UUID

from pydantic import BaseModel

from .define_parser import (
    _PUNCT_TOKEN,
    _WORD_TOKEN,
    DefineParseError,
    Kind,
    KLiteral,
    KName,
    KObject,
    KTuple,
    KUnion,
    _flat_members,
    _lex,
    _scan,
    parse_kind,
    unquote,
)
from .edge import BaseSurrealEdge
from .functions import SurrealFunc
from .model_base import BaseSurrealModel
from .schema_state import FieldState, IndexState, SchemaIssue, SchemaState, TableState
from .types import Duration
from .utils import _KEEP_LIMIT, VALID_ALIAS_PATTERN

__all__ = ["ModelCodeGenerator"]

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


def _is_edge(table: TableState) -> bool:
    """Whether ``table`` is a relation table, whatever the case of its ``table_type`` (the reader writes it lower-case,
    a hand-built state may not): its class is a ``BaseSurrealEdge``, which provides ``in_`` and ``out``."""
    return table.table_type.lower() == "relation"


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
        provided = _EDGE_PROVIDED if _is_edge(table) else _MODEL_PROVIDED
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
    skipped = {"id", "in", "out"} if _is_edge(table) else {"id"}
    return sorted({field.path[0] for field in table.fields.values() if field.path} - skipped)


def _children(nodes: Mapping[tuple[str, ...], FieldState], path: tuple[str, ...]) -> list[str]:
    """The declared direct children of ``path`` (not its items), in codepoint order."""
    depth = len(path) + 1
    return sorted(child[-1] for child in nodes if len(child) == depth and child[: len(path)] == path and child[-1] != "*")


# --- the generated module (spec §6.4.3–§6.4.8) ----------------------------------------------------------------------

#: The generated module's docstring (spec §6.4.8).
_DOCSTRING = (
    '"""Models generated by surreal_orm_lite.generate_models_from_db() from a SurrealDB schema.\n'
    "\n"
    "Review them before use. They describe tables that already exist: do not call\n"
    "define_computed_fields() on them, it would replace the database's field definitions.\n"
    '"""'
)

#: Why a table or a field is not generated (spec §6.4.8: the header's skip lines S2–S5; S1 and S6 are above).
_S2_NOT_READ = "it could not be read (see SchemaState.issues)"
_S3_NOT_PARSED = "its definition could not be parsed (see SchemaState.issues)"
_S4_NOT_PARSED = _S3_NOT_PARSED
_S4_SAME_NAME = "another definition has the same name (see SchemaState.issues)"
_S5_NO_PARENT = "its parent is not defined (the parent is a dict[str, Any])"

#: The reader's issue texts the generator tells apart (spec §5.7): I3 keeps the field (typed ``Any``), I5 drops the
#: second definition.
_I3_TYPE_AS_WRITTEN = "its TYPE clause was not understood and is kept as written"
_I5_SAME_NAME = "two definitions have the same name"

#: The class comment of a table with a ``READONLY`` field holding a datetime ``update()`` may not keep (spec §6.4.7, M29).
#: Since #261 ``update()`` keeps every stored datetime it did not change at the top level, up to 32 (``_KEEP_LIMIT``).
#: Below the top level it keeps one only while the values it rebuilds to reach it fit in what the fields before it left
#: of those 32, which depends on the data (a list of timestamped items is enough), so that is no guarantee
#: (``replace_content``).
_UPDATE_NOTE = (
    "update() may fail on READONLY datetimes below the top level or past the 32nd (Python keeps microseconds): use merge()."
)
_SCHEMALESS_NOTE = 'SCHEMALESS: fields not declared here are kept (extra="allow")'
#: The operations of a table's ``PERMISSIONS``, in the order SurrealDB renders them.
_OPERATIONS = ("select", "create", "update", "delete")

#: Where each importable name comes from; the blocks in isort order (spec §6.4.8).
_STANDARD_MODULES = ("datetime", "decimal", "typing", "uuid")
_THIRD_PARTY_MODULES = ("pydantic", "surreal_orm_lite", "surrealdb")
_IMPORTED_FROM: Mapping[str, str] = {
    **dict.fromkeys(_IMPORTABLE, "surreal_orm_lite"),
    "datetime": "datetime",
    "Decimal": "decimal",
    "Any": "typing",
    "Literal": "typing",
    "UUID": "uuid",
    "BaseModel": "pydantic",
    "ConfigDict": "pydantic",
    "Field": "pydantic",
    "RecordID": "surrealdb",
}
#: An import that fits stays on one line (spec §6.4.8).
_IMPORT_WIDTH = 88

_INT_TEXT = re.compile(r"-?[0-9]+")
_FLOAT_TEXT = re.compile(r"-?[0-9]+(?:\.[0-9]+)?f")
_DECIMAL_TEXT = re.compile(r"-?[0-9]+(?:\.[0-9]+)?dec")
#: A datetime ``DEFAULT`` the model holds exactly: UTC, at most 6 fractional digits — ``datetime.fromisoformat()``
#: truncates more without an error (Python 3.11 too), and the server keeps nanoseconds (spec §6.4.6 item 3, M48).
_DATETIME_TEXT = re.compile(r"d'([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z)'")
#: An empty object ``DEFAULT``: only whitespace between the braces (the servers render ``{}`` as ``{  }``).
_EMPTY_OBJECT_TEXT = re.compile(r"\{\s*\}")
#: How deep arrays may nest in a ``DEFAULT`` the generator copies; deeper ones keep the server form.
_MAX_DEFAULT_DEPTH = 32

#: ``geometry<k>`` kind → the ORM class, and the class → its nullable alias for a top-level field (spec §6.4.4).
_GEOMETRY_CLASSES: Mapping[str, str] = {
    "point": "GeometryPoint",
    "line": "GeometryLine",
    "polygon": "GeometryPolygon",
    "multipoint": "GeometryMultiPoint",
    "multiline": "GeometryMultiLine",
    "multipolygon": "GeometryMultiPolygon",
    "collection": "GeometryCollection",
}
_GEOMETRY_ALIASES: Mapping[str, str] = {
    "GeometryPoint": "PointField",
    "GeometryLine": "LineStringField",
    "GeometryPolygon": "PolygonField",
    "GeometryMultiPoint": "MultiPointField",
    "GeometryMultiLine": "MultiLineStringField",
    "GeometryMultiPolygon": "MultiPolygonField",
    "GeometryCollection": "GeometryCollectionField",
    "Geometry": "GeometryField",
}

#: The member types a ``set[T]`` may hold: hashable values (spec §6.4.4, README "Sets").
_HASHABLE = frozenset({"str", "int", "float", "bool", "bytes", "Decimal", "UUID", "datetime", "Duration"})

_LiteralValue = str | int | bool


class _Type(NamedTuple):
    """An annotation as a union: its member texts in order — ``""`` marks where the one ``Literal[...]`` group goes —
    the group's values, and whether ``None`` is a member (always written last)."""

    members: tuple[str, ...] = ()
    literals: tuple[_LiteralValue, ...] = ()
    none: bool = False

    @property
    def is_any(self) -> bool:
        return self.members == ("Any",)

    def holds(self, value: _LiteralValue) -> bool:
        """Whether the ``Literal[...]`` group holds ``value`` (``True`` is not ``1``)."""
        return any(type(item) is type(value) and item == value for item in self.literals)

    def nullable(self) -> "_Type":
        """This annotation with ``None`` (``Any`` already accepts it)."""
        return self if self.is_any else self._replace(none=True)

    def text(self) -> str:
        parts = [_literal_text(self.literals) if member == "" else member for member in self.members]
        if self.none:
            parts.append("None")
        return " | ".join(parts) or "None"


_ANY = _Type(("Any",))
_NONE = _Type(none=True)

#: Named kinds with a fixed annotation (spec §6.4.4); a name not here (``regex``, ``function``, ``file``, an unknown
#: one) is ``Any``.
_NAMED_TYPES: Mapping[str, _Type] = {
    "any": _ANY,
    "bool": _Type(("bool",)),
    "int": _Type(("int",)),
    "float": _Type(("float",)),
    "string": _Type(("str",)),
    "number": _Type(("int", "float", "Decimal")),
    "decimal": _Type(("Decimal",)),
    "datetime": _Type(("datetime",)),
    "duration": _Type(("Duration",)),
    "uuid": _Type(("UUID",)),
    "bytes": _Type(("bytes",)),
    "range": _Type(("Range",)),
    "record": _Type(("RecordID",)),
}


def _literal_text(values: Sequence[_LiteralValue]) -> str:
    """``Literal[...]`` of ``values``: strings through ``_py_str``, integers and booleans with ``repr``."""
    return "Literal[" + ", ".join(_py_str(value) if isinstance(value, str) else repr(value) for value in values) + "]"


def _union(types: Iterable[_Type]) -> _Type:
    """The union of ``types``: members in order without duplicates, one ``Literal`` group, ``Any`` absorbing all."""
    members: list[str] = []
    literals: list[_LiteralValue] = []
    none = False
    for item in types:
        none = none or item.none
        members.extend(member for member in item.members if member not in members)
        for value in item.literals:
            if not any(type(seen) is type(value) and seen == value for seen in literals):
                literals.append(value)
    if "Any" in members:
        return _ANY
    return _Type(tuple(members), tuple(literals), none)


def _literal_type(text: str) -> _Type:
    """The annotation of a literal kind as rendered: ``'a'``, ``-2``, ``true`` → ``Literal``; ``1.5f`` → ``float``;
    ``1.5dec`` → ``Decimal``; ``none``/``null`` → ``None``."""
    if text[:1] in ("'", '"'):  # parse_kind() read it whole: one literal
        return _Type(("",), (unquote(text),))
    if text in ("true", "false"):
        return _Type(("",), (text == "true",))
    if text in ("none", "null"):
        return _NONE
    if _INT_TEXT.fullmatch(text):
        try:
            return _Type(("",), (int(text),))
        except ValueError:  # more digits than int() reads
            return _Type(("int",))
    return _Type(("Decimal",)) if text.endswith("dec") else _Type(("float",))


def _geometry_classes(kind: KName) -> tuple[str, ...]:
    """The ORM classes of ``geometry<a | b>``: ``Geometry`` for any kind (no argument, ``feature``, an unknown one)."""
    names = [argument.lower() for argument in kind.args if isinstance(argument, str)]
    if not names or len(names) != len(kind.args) or any(name not in _GEOMETRY_CLASSES for name in names):
        return ("Geometry",)
    return tuple(dict.fromkeys(_GEOMETRY_CLASSES[name] for name in names))


def _hashable(item: _Type) -> bool:
    """Whether a ``set[...]`` of ``item`` is possible: every member hashable (a ``Literal`` group is)."""
    return not item.is_any and bool(item.members or item.none) and all(m == "" or m in _HASHABLE for m in item.members)


def _datetime_below_the_top(kind: Kind | None, depth: int) -> bool:
    """Whether the kind of a value ``depth`` levels below the top level holds a ``datetime`` below the top level.

    That is a datetime anywhere but as the value of a top-level field itself: in a sub-object, a tuple, an array or a
    set (``{ at: datetime }``, ``[int, datetime]``, ``array<datetime>``, ``option<set<datetime>>``,
    ``string | array<datetime>`` …). ``update()`` keeps a stored datetime it did not change there only while the
    budget the fields before it left allows, which depends on the data (#261, ``replace_content``).
    """
    if isinstance(kind, KName):
        if kind.name == "datetime":
            return depth > 0
        return any(_datetime_below_the_top(argument, depth + 1) for argument in kind.args if not isinstance(argument, str))
    if isinstance(kind, KUnion):
        return any(_datetime_below_the_top(member, depth) for member in kind.members)
    if isinstance(kind, KTuple):
        return any(_datetime_below_the_top(item, depth + 1) for item in kind.items)
    if isinstance(kind, KObject):
        return any(_datetime_below_the_top(value, depth + 1) for _, value in kind.items)
    return False


def _may_be_a_datetime(kind: Kind | None) -> bool:
    """Whether a field of this kind may hold a ``datetime`` as its own value: ``datetime``, ``any``, a union holding
    one of them (``option<datetime>``, ``string | datetime``), or a kind kept as written (I3), which may be anything."""
    if isinstance(kind, KName):
        return kind.name in ("datetime", "any")
    if isinstance(kind, KUnion):
        return any(_may_be_a_datetime(member) for member in kind.members)
    return kind is None


def _reads_input(expression: str) -> bool:
    """Whether a ``VALUE`` expression reads ``$value`` or ``$input`` (outside strings): then the field is written and
    the server transforms it; otherwise the server computes it."""
    try:
        lexemes = _lex(expression, 0)
    except DefineParseError:  # not scannable (a hand-built state): search the text
        return "$value" in expression or "$input" in expression
    for (kind, start, _), (following, begin, end) in zip(lexemes, lexemes[1:], strict=False):
        if (
            kind == _PUNCT_TOKEN
            and expression[start] == "$"
            and following == _WORD_TOKEN
            and expression[begin:end] in ("value", "input")
        ):
            return True
    return False


def _computed_expression(field: FieldState) -> str | None:
    """The expression the server computes the field from: ``COMPUTED e``, or ``VALUE e`` not reading its input."""
    if field.computed is not None:
        return field.computed
    if field.value is not None and not _reads_input(field.value):
        return field.value
    return None


def _computable(expression: str) -> bool:
    """Whether ``computed()`` accepts ``expression`` (``SurrealFunc`` refuses an empty text and any ``;``)."""
    try:
        SurrealFunc(expression)
    except ValueError:
        return False
    return True


def _python_default(text: str, annotation: _Type) -> str | None:
    """The Python default of a literal ``DEFAULT`` whose value matches the annotation — the scalar rows of spec §6.4.6
    item 3 (D25) — as source; ``None`` when there is none: :meth:`_ClassWriter.constant` tries the other rows."""
    members = frozenset(annotation.members)
    is_any = annotation.is_any
    if _INT_TEXT.fullmatch(text):
        try:
            number = int(text)
        except ValueError:  # more digits than int() reads
            return None
        if is_any or "int" in members:
            return repr(number)
        if "float" in members:
            try:
                as_float = float(number)
            except OverflowError:
                return None
            return repr(as_float) if as_float == number else None
        return "Decimal(" + _py_str(text) + ")" if "Decimal" in members else None
    if _FLOAT_TEXT.fullmatch(text):
        value = float(text[:-1])
        return repr(value) if (is_any or "float" in members) and math.isfinite(value) else None
    if _DECIMAL_TEXT.fullmatch(text):
        return "Decimal(" + _py_str(text[:-3]) + ")" if "Decimal" in members else None
    if text[:1] in ("'", '"'):
        try:
            string = unquote(text)
        except DefineParseError:
            return None
        return _py_str(string) if is_any or "str" in members or annotation.holds(string) else None
    if text in ("true", "false"):
        flag = text == "true"
        return repr(flag) if is_any or "bool" in members or annotation.holds(flag) else None
    if text in ("NONE", "NULL"):
        return "None" if is_any or annotation.none else None
    return None


class _Default(NamedTuple):
    """A Python default as source: ``expression`` builds the value; ``factory`` is what ``default_factory`` takes when
    the value is mutable (``list``, ``lambda: [1, 2]``, a sub-model class …), ``None`` for a plain default."""

    expression: str
    factory: str | None = None


#: What :meth:`_ClassWriter.constant` answers for ``{  }`` on a sub-model that cannot be built without arguments: the
#: field has no default (spec §6.4.6 item 3, M48: the server refuses every create that leaves it to that ``DEFAULT``).
_REQUIRED = _Default("")

_NONE_DEFAULT = _Default("None")


def _array_elements(text: str) -> list[str] | None:
    """The elements of one SurrealQL array literal, split at its top-level commas and stripped (``[]`` → ``[]``);
    ``None`` when ``text`` is not exactly one array literal, or an element is empty."""
    if not (text.startswith("[") and text.endswith("]")):
        return None
    try:
        tokens = _scan(text)
    except DefineParseError:
        return None
    inside = tokens[1:-1]
    if any(not token.depth for token in inside):  # the opening bracket closes before the end: not one literal
        return None
    elements: list[str] = []
    begin = 1
    for token in inside:
        if token.kind == _PUNCT_TOKEN and token.depth == 1 and text[token.start] == ",":
            elements.append(text[begin : token.start].strip())
            begin = token.end
    last = text[begin:-1].strip()
    if not elements and not last:
        return []
    elements.append(last)
    return None if not all(elements) else elements


def _datetime_default(text: str) -> str | None:
    """``datetime.fromisoformat("…")`` for a ``d'…'`` the model holds exactly (UTC, at most 6 fractional digits)."""
    match = _DATETIME_TEXT.fullmatch(text)
    if match is None:
        return None
    try:
        datetime.fromisoformat(match.group(1))
    except ValueError:  # a date that does not exist
        return None
    return "datetime.fromisoformat(" + _py_str(match.group(1)) + ")"


def _uuid_default(text: str) -> str | None:
    """``UUID("…")`` for a ``u'…'`` in the canonical form ``str(UUID(…))`` writes back."""
    if len(text) < 3 or not text.startswith("u'") or not text.endswith("'"):
        return None
    inner = text[2:-1]
    try:
        canonical = str(UUID(inner))
    except ValueError:
        return None
    return "UUID(" + _py_str(inner) + ")" if canonical == inner else None


def _duration_default(text: str) -> _Default | None:
    """``Duration.parse("…")`` — a factory, ``Duration`` being mutable — for a duration ``Duration.parse()`` accepts."""
    try:
        Duration.parse(text)
    except ValueError:
        return None
    expression = "Duration.parse(" + _py_str(text) + ")"
    return _Default(expression, "lambda: " + expression)


def _kind_members(kind: Kind | None) -> list[Kind]:
    """The members of a union kind, flattened; the kind itself otherwise (none for ``None``)."""
    if kind is None:
        return []
    return _flat_members(kind.members) if isinstance(kind, KUnion) else [kind]


def _width(line: str) -> int:
    """The display width of ``line`` as ruff measures it: 2 per East Asian wide or full-width character, else 1."""
    return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in line)


def _import_key(name: str) -> tuple[int, str, str]:
    """isort's order of the names of one ``from`` import: constants, then classes, then the rest, case-insensitive."""
    kind = 0 if len(name) > 1 and name.isupper() else 1 if name[0].isupper() else 2
    return kind, name.lower(), name


def _imports(body: str) -> str:
    """The import blocks of the names ``body`` uses (its name tokens: strings and comments never count)."""
    used: dict[str, set[str]] = {}
    for token in tokenize.generate_tokens(io.StringIO(body).readline):
        module = _IMPORTED_FROM.get(token.string) if token.type == tokenize.NAME else None
        if module is not None:
            used.setdefault(module, set()).add(token.string)
    blocks = []
    for modules in (_STANDARD_MODULES, _THIRD_PARTY_MODULES):
        lines = []
        for module in modules:
            if module not in used:
                continue
            names = sorted(used[module], key=_import_key)
            line = f"from {module} import {', '.join(names)}"
            if len(line) > _IMPORT_WIDTH:
                line = f"from {module} import (\n" + "".join(f"    {name},\n" for name in names) + ")"
            lines.append(line)
        if lines:
            blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


class _Annotation(NamedTuple):
    """A field's annotation: its text, its members (for the default), and the text without the field's own
    nullability — what ``Computed[...]`` wraps, since ``Computed[T]`` is optional already."""

    text: str
    type: _Type
    base: str


class _Line(NamedTuple):
    """A field's line (without indentation) and what the class needs to know about it."""

    text: str
    #: Listed in ``server_fields`` (a model's field in the server form).
    server: bool = False
    #: ``Computed[T] = computed(…)``: never written by the ORM.
    computed: bool = False
    #: No default: the field must be given.
    required: bool = False


class _ClassWriter:
    """The source of one table: its sub-models, then its class (spec §6.4.5–§6.4.7)."""

    def __init__(self, table: TableState, names: _Names, connection: str) -> None:
        self.table = table
        self.names = names
        self.connection = connection
        self.edge = _is_edge(table)
        self.schemaless = table.schema_mode.upper() == "SCHEMALESS"
        self.nodes = {tuple(field.path): field for field in table.fields.values()}
        self.blocks: list[str] = []
        #: ``(table, field, line)`` of every field not generated (S5).
        self.skips: list[tuple[str, str, str]] = []
        #: Path → the sub-model written there (``None``: a ``dict[str, Any]``), so that each is written once.
        self.sub_models_at: dict[tuple[str, ...], str | None] = {}
        #: Sub-model class → whether it can be built without arguments (every field has a default).
        self.buildable: dict[str, bool] = {}

    def write(self) -> list[str]:
        """The sub-model blocks, then the class block."""
        lines: list[str] = []
        server_fields: list[str] = []
        for column, (attribute, alias) in self.names.fields(self.table.name).items():
            node = self.nodes.get((column,))
            if node is None:
                lines.append(_assigned(f"{attribute}: dict[str, Any] | None", _NONE_DEFAULT, alias))
                self._skip_children(column)
                continue
            line = self.field_line(attribute, alias, node, model=True)
            lines.append(line.text)
            if line.server:
                server_fields.append(attribute)
        base = "BaseSurrealEdge" if self.edge else "BaseSurrealModel"
        block = [f"class {self.names.class_name(self.table.name)}({base}):"]
        block.extend("    " + comment for comment in self._comments())
        block.extend(self._model_config(server_fields))
        body = lines if self.edge else ["id: str | None = None", *lines]
        if body:
            block.append("")
            block.extend("    " + line for line in body)
        return [*self.blocks, "\n".join(block)]

    def _skip_children(self, column: str) -> None:
        """S5: the fields under the undefined top-level ``column``."""
        for field in sorted(self.table.fields.values(), key=lambda field: field.name):
            if field.path and field.path[0] == column:
                line = "- field " + repr(self.table.name + "." + field.name) + ": " + _S5_NO_PARENT
                self.skips.append((self.table.name, field.name, line))

    def _comments(self) -> list[str]:
        """The class comments of spec §6.4.7, each through ``_comment()``."""
        table = self.table
        texts = []
        if table.comment is not None:
            texts.append("Comment: " + table.comment)
        if self.edge:
            ends = " | ".join(table.relation_in) or "any", " | ".join(table.relation_out) or "any"
            texts.append("IN " + ends[0] + " OUT " + ends[1] + (" ENFORCED" if table.enforced else ""))
        if table.view_query is not None:
            texts.append("View: AS " + table.view_query)
        if table.changefeed is not None:
            texts.append("Changefeed: " + table.changefeed + (" INCLUDE ORIGINAL" if table.changefeed_include_original else ""))
        permissions = _permissions_text(table.permissions)
        if permissions is not None:
            texts.append("Permissions: " + permissions)
        if self.schemaless:
            texts.append(_SCHEMALESS_NOTE)
        if table.indexes:
            texts.append("Indexes: " + ", ".join(_index_label(index) for index in table.indexes.values()))
        if table.events:
            texts.append("Events: " + ", ".join(event.name for event in table.events.values()))
        if self._readonly_datetime_not_kept():
            texts.append(_UPDATE_NOTE)
        return [_comment(text, indent=4) for text in texts]

    def _readonly_datetime_not_kept(self) -> bool:
        """Whether a ``READONLY`` field of the class — top level or nested — holds a datetime ``update()`` may not keep
        (M29, #261): one below the top level, in its kind or in a sub-field's (``address.at``, ``items.*.at``,
        ``_datetime_below_the_top``), or one at the top level of a class with more than ``_KEEP_LIMIT`` top-level fields
        that may hold a datetime (``_may_be_a_datetime``), since ``update()`` keeps the first ones only."""
        provided = {"id", "in", "out"} if self.edge else {"id"}
        fields = [field for field in self.table.fields.values() if field.path and field.path[0] not in provided]
        readonly = [field for field in fields if field.readonly]
        if any(
            _datetime_below_the_top(_kind(inner), len(inner.path) - 1)
            for field in readonly
            for inner in fields
            if tuple(inner.path[: len(field.path)]) == tuple(field.path)
        ):
            return True
        top = [field for field in fields if len(field.path) == 1 and _may_be_a_datetime(_kind(field))]
        return len(top) > _KEEP_LIMIT and any(field.readonly for field in top)

    def _model_config(self, server_fields: Sequence[str]) -> list[str]:
        """The ``model_config`` line, wrapped as ``ruff format`` keeps it above 128 columns (spec §6.4.7, D20)."""
        keywords = ["table_name=" + _py_str(self.table.name)]
        if self.connection != "default":
            keywords.append("connection=" + _py_str(self.connection))
        listed = [_py_str(attribute) for attribute in server_fields]
        if listed:
            keywords.append("server_fields=[" + ", ".join(listed) + "]")
        if self.schemaless:
            keywords.append('extra="allow"')
        one_line = "    model_config = SurrealConfigDict(" + ", ".join(keywords) + ")"
        if _width(one_line) <= _MAX_WIDTH:
            return [one_line]
        lines = ["    model_config = SurrealConfigDict("]
        for keyword_text in keywords:
            line = "        " + keyword_text + ","
            if keyword_text.startswith("server_fields=") and _width(line) > _MAX_WIDTH:
                lines.append("        server_fields=[")
                lines.extend("            " + name + "," for name in listed)
                lines.append("        ],")
            else:
                lines.append(line)
        lines.append("    )")
        return lines

    def field_line(self, attribute: str, alias: str | None, field: FieldState, *, model: bool) -> _Line:
        """The line declaring ``field`` (without indentation), and whether it is a server field, a computed one or a
        required one (spec §6.4.6).

        ``model`` is false for a sub-model's field: ``Computed`` and ``server_fields`` belong to models, so a computed
        or server-filled child takes the server form without being listed.
        """
        annotation = self.annotation(field, top=model)
        text = annotation.text
        default: _Default | None = None
        server = False
        expression = _computed_expression(field)
        if expression is not None:
            if model and alias is None and _computable(expression):
                line = f"{attribute}: Computed[{annotation.base}] = computed({_py_str(expression)})"
                return _Line(_readonly(line, field), computed=True)
            server = True
        elif field.default is not None:
            default = self.constant(field.default, _kind(field), annotation.type, tuple(field.path))
            if default is None:
                server = True
            if default is _REQUIRED:
                default = None
        elif field.nullable or annotation.type.is_any:
            default = _NONE_DEFAULT
        if server:
            if not (annotation.type.none or annotation.type.is_any):
                text += " | None"
            default = _NONE_DEFAULT
        line = _readonly(_assigned(f"{attribute}: {text}", default, alias), field)
        return _Line(line, server=server and model, required=default is None)

    def constant(
        self, text: str, kind: Kind | None, annotation: _Type, path: tuple[str, ...] | None, depth: int = 0
    ) -> _Default | None:
        """The Python default of a constant ``DEFAULT`` the model holds exactly (spec §6.4.6 item 3, D25, M48); ``None``
        when there is none (the server form); :data:`_REQUIRED` for ``{  }`` on a sub-model that needs arguments.

        ``kind`` and ``path`` (a field's own, or ``None``) find an array's items and their sub-model; ``depth`` counts the
        arrays around ``text``.
        """
        scalar = _python_default(text, annotation)
        if scalar is not None:
            return _Default(scalar)
        members = frozenset(annotation.members)
        is_any = annotation.is_any
        if text.startswith("["):
            return self._array(text, kind, annotation, path, depth)
        if _EMPTY_OBJECT_TEXT.fullmatch(text):
            if is_any or "dict[str, Any]" in members:
                return _Default("{}", "dict")
            for member in annotation.members:
                if member in self.buildable:
                    return _Default(member + "()", member) if self.buildable[member] else _REQUIRED
            return None
        if is_any or "Duration" in members:
            duration = _duration_default(text)
            if duration is not None:
                return duration
        if is_any or "datetime" in members:
            moment = _datetime_default(text)
            if moment is not None:
                return _Default(moment)
        if is_any or "UUID" in members:
            identifier = _uuid_default(text)
            if identifier is not None:
                return _Default(identifier)
        return None

    def _array(
        self, text: str, kind: Kind | None, annotation: _Type, path: tuple[str, ...] | None, depth: int
    ) -> _Default | None:
        """``[]`` on a ``list[…]``, a ``set[…]`` or ``Any``; ``[e1, …]`` on an ``array`` kind (or ``Any``) whose item
        annotation holds every element (spec §6.4.6 item 3). A non-empty array on a set, whose order and duplicates
        the server changes, or on a tuple keeps the server form."""
        elements = _array_elements(text) if depth < _MAX_DEFAULT_DEPTH else None
        if elements is None:
            return None
        if not elements:
            if annotation.is_any or any(member.startswith("list[") for member in annotation.members):
                return _Default("[]", "list")
            if any(member.startswith("set[") for member in annotation.members):
                return _Default("set()", "set")
            return None
        item_kind: Kind | None = None
        item_type = _ANY
        if not annotation.is_any:
            arrays = [member for member in _kind_members(kind) if isinstance(member, KName) and member.name == "array"]
            if len(arrays) != 1:
                return None
            item_kind = _array_item(arrays[0])
            # The items' sub-model, when the field's own kind is this array (as kind_type() found it).
            item_model = self.sub_model((*path, "*")) if path is not None and arrays[0] is kind else None
            if item_model is not None:
                item_type = _Type((item_model,))
            elif item_kind is not None:
                item_type = self.kind_type(item_kind, None)
        values = []
        for element in elements:
            value = self.constant(element, item_kind, item_type, None, depth + 1)
            if value is None or value is _REQUIRED:
                return None
            values.append(value.expression)
        expression = "[" + ", ".join(values) + "]"
        return _Default(expression, "lambda: " + expression)

    def annotation(self, field: FieldState, *, top: bool) -> _Annotation:
        """The annotation of ``field`` (spec §6.4.4): a top-level geometry of one kind takes its nullable alias."""
        kind = _kind(field)
        path = tuple(field.path)
        if top and isinstance(kind, KName) and kind.name == "geometry":
            classes = _geometry_classes(kind)
            base = _Type(classes)
            if not field.nullable:
                return _Annotation(base.text(), base, base.text())
            if len(classes) == 1:
                return _Annotation(_GEOMETRY_ALIASES[classes[0]], base.nullable(), base.text())
            return _Annotation(base.nullable().text(), base.nullable(), base.text())
        own = self.kind_type(kind, path)
        full = own.nullable() if field.nullable else own
        return _Annotation(full.text(), full, own.text())

    def kind_type(self, kind: Kind | None, path: tuple[str, ...] | None) -> _Type:
        """The annotation of a kind; ``path`` (a field's own kind only) finds its sub-model or its items' sub-model."""
        if kind is None:  # I3: the TYPE kept as written
            return _ANY
        if isinstance(kind, KUnion):
            return _union(self.kind_type(member, None) for member in kind.members)
        if isinstance(kind, KLiteral):
            return _literal_type(kind.text)
        if isinstance(kind, KObject):
            return _Type(("dict[str, Any]",))
        if isinstance(kind, KTuple):
            return _Type(("tuple[" + ", ".join(self.kind_type(item, None).text() for item in kind.items) + "]",))
        if kind.name == "object":
            sub_model = self.sub_model(path) if path is not None else None
            return _Type((sub_model or "dict[str, Any]",))
        if kind.name in ("array", "set"):
            if kind.name == "array" and path is not None:
                item_model = self.sub_model((*path, "*"))
                if item_model is not None:
                    return _Type((f"list[{item_model}]",))
            item_kind = _array_item(kind)
            item = self.kind_type(item_kind, None) if item_kind is not None else _ANY
            container = "set" if kind.name == "set" and _hashable(item) else "list"
            return _Type((f"{container}[{item.text()}]",))
        if kind.name == "geometry":
            return _Type(_geometry_classes(kind))
        return _NAMED_TYPES.get(kind.name, _ANY)

    def sub_model(self, path: tuple[str, ...]) -> str | None:
        """The sub-model class at ``path``, written to ``blocks`` (after its own sub-models) the first time it is asked
        for; ``None`` when there is no candidate there or a child cannot be one of its fields as it is (spec §6.4.5)."""
        if path in self.sub_models_at:
            return self.sub_models_at[path]
        name = self.names.sub_models.get((self.table.name, path))
        children = _children(self.nodes, path)
        if name is None or not children or not all(self.names.usable_child(child) for child in children):
            self.sub_models_at[path] = None
            return None
        self.sub_models_at[path] = name
        node = self.nodes.get(path)
        lines = [f"class {name}(BaseModel):"]
        if self.schemaless or (node is not None and node.flexible):
            lines.extend(['    model_config = ConfigDict(extra="allow")', ""])
        buildable = True
        for child in children:
            line = self.field_line(child, None, self.nodes[(*path, child)], model=False)
            lines.append("    " + line.text)
            buildable = buildable and not line.required
        self.buildable[name] = buildable
        self.blocks.append("\n".join(lines))
        return name


def _assigned(declaration: str, default: _Default | None, alias: str | None) -> str:
    """``declaration`` with its default — ``= <default>``, ``Field(default_factory=…)`` for a mutable one, and
    ``Field(…, alias=…)`` for an aliased column (spec §6.4.6 item 5)."""
    arguments = []
    if default is not None:
        arguments.append("default=" + default.expression if default.factory is None else "default_factory=" + default.factory)
    if alias is not None:
        arguments.append("alias=" + _py_str(alias))
    if alias is None and default is not None and default.factory is None:
        return declaration + " = " + default.expression
    return declaration + " = Field(" + ", ".join(arguments) + ")" if arguments else declaration


def _readonly(line: str, field: FieldState) -> str:
    """``line`` with the trailing ``# READONLY`` comment of a ``READONLY`` field (no ``Field`` option exists)."""
    return line + "  # READONLY" if field.readonly else line


def _permissions_text(permissions: Mapping[str, str]) -> str | None:
    """A table's ``PERMISSIONS`` as SurrealDB renders them: ``FULL`` when one rule covers the four operations, else
    ``FOR select WHERE …, FOR create, update FULL`` — operations in SurrealQL's order, grouped by rule in the order the
    rules first appear. ``None`` when there is none, or every rule is ``NONE`` (a table's default)."""
    if all(rule == "NONE" for rule in permissions.values()):
        return None
    order = {operation: position for position, operation in enumerate(_OPERATIONS)}
    groups: dict[str, list[str]] = {}
    for operation in sorted(permissions, key=lambda operation: (order.get(operation, len(order)), operation)):
        groups.setdefault(permissions[operation], []).append(operation)
    if len(groups) == 1 and permissions.keys() == order.keys():
        return next(iter(groups))
    return ", ".join("FOR " + ", ".join(operations) + " " + rule for rule, operations in groups.items())


def _index_label(index: IndexState) -> str:
    """``name (UNIQUE)``, ``name (FULLTEXT)`` … — the index's name and its kind, when it has one."""
    for flag, label in (
        (index.unique, "UNIQUE"),
        (index.search_analyzer is not None, "FULLTEXT"),
        (index.hnsw, "HNSW"),
        (index.mtree, "MTREE"),
        (index.count, "COUNT"),
    ):
        if flag:
            return index.name + " (" + label + ")"
    return index.name


def _skipped_tables(state: SchemaState) -> dict[str, str]:
    """Table name → why it is not generated: S1 or S6 by its name, else S2 (``INFO FOR TABLE`` failed: the table is
    kept without definitions) or S3 (its definition could not be parsed: it is not in ``state.tables``)."""
    present = {table.name for table in state.tables.values()}
    with_issue = {issue.name for issue in state.issues if issue.kind == "table"}
    reasons = {}
    for name in sorted(present | with_issue):
        reason = _skip_reason(name)
        if reason is None and name in with_issue:
            reason = _S2_NOT_READ if name in present else _S3_NOT_PARSED
        if reason is not None:
            reasons[name] = reason
    return reasons


def _field_skip(issue: SchemaIssue) -> tuple[str, str, str] | None:
    """S4: a field issue that left a field out (I1, I5); ``None`` for I3, whose field is generated (typed ``Any``)."""
    if issue.kind != "field" or issue.table is None or issue.message == _I3_TYPE_AS_WRITTEN:
        return None
    reason = _S4_SAME_NAME if issue.message == _I5_SAME_NAME else _S4_NOT_PARSED
    return issue.table, issue.name, "- field " + repr(issue.table + "." + issue.name) + ": " + reason


class ModelCodeGenerator:
    """Generate the Python source of models from a schema — the full SurrealDB-ORM's ``ModelCodeGenerator`` (v0.30.0).

    ::

        schema = await DatabaseIntrospector("analytics").introspect()
        source = ModelCodeGenerator().generate(schema)

    One ``BaseSurrealModel`` class per table (``BaseSurrealEdge`` for a relation table) whose name is an identifier the
    ORM can query, in codepoint order of the table names, each preceded by its sub-models. The module imports and
    passes ``ruff check`` and ``ruff format`` (line length 128) as long as its lines fit: comments are cut to 128
    columns and ``model_config`` is wrapped, but a long name or ``Literal`` union stays on one line. Tables and fields
    that are not generated are listed, with the reason, in the module's ``# Not generated:`` comment.

    Review the result before use: ``ASSERT``, ``PERMISSIONS`` and ``REFERENCE`` clauses are not generated,
    ``READONLY`` is a comment, and the models describe tables that exist — never call ``define_computed_fields()`` on
    them.
    """

    __slots__ = ()

    def generate(self, state: SchemaState) -> str:
        """Return the source of one module for ``state`` (spec §6.4), ending with a newline.

        Pure and synchronous: the same state gives the same string. Server text — names, comments, expressions —
        reaches the source only as string literals, comments and names the generator builds (spec §8).
        """
        skipped = _skipped_tables(state)
        tables = sorted(
            (table for table in state.tables.values() if table.name not in skipped),
            key=lambda table: table.name,
        )
        names = _Names(tables)
        skips = [(name, "", "- table " + repr(name) + ": " + reason) for name, reason in skipped.items()]
        blocks: list[str] = []
        for table in tables:
            writer = _ClassWriter(table, names, state.connection)
            blocks.extend(writer.write())
            skips.extend(writer.skips)
        generated = {table.name for table in tables}
        for issue in state.issues:
            skip = _field_skip(issue)
            if skip is not None and skip[0] in generated:
                skips.append(skip)
        body = "\n\n\n".join(blocks)
        header = [_DOCSTRING]
        imports = _imports(body)
        if imports:
            header.append(imports)
        if skips:
            lines = [_comment("Not generated:", indent=0)]
            lines.extend(_comment(text, indent=0) for _, _, text in sorted(skips))
            header.append("\n".join(lines))
        source = "\n\n".join(header)
        if body:
            source += "\n\n\n" + body
        return source + "\n"
