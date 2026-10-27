"""The schema of a SurrealDB database as immutable typed objects (v0.30.0), as ``DatabaseIntrospector`` reads it.

Every class is a frozen, slotted dataclass: an attribute cannot be reassigned, and the containers it holds are
read-only too — a mapping is a read-only map that iterates in sorted key order (codepoint order), compares equal to
any mapping with the same items and supports ``copy``, ``deepcopy`` and ``pickle``; a sequence is a ``tuple``.
States holding a mapping are not hashable (as the full ORM's mutable dataclasses).

**Construction parity with the full SurrealDB-ORM.** The seven classes it has (``SchemaState``, ``TableState``,
``FieldState``, ``IndexState``, ``EventState``, ``AccessState``, ``AnalyzerState``) keep its attribute names,
positional order, defaults and accepted inputs (a ``list`` becomes a ``tuple``, a ``dict`` a read-only map, the
pipe-joined ``relation_in``/``relation_out`` string a ``tuple`` of names). The lite additions come after a
``KW_ONLY`` marker: they are keyword-only. Where the full ORM has an attribute the lite does not provide
(``FieldState.encrypted``, ``TableState.access``, ``SchemaState.applied_migrations``), every later attribute is
keyword-only, so a full-ORM positional call that reaches it raises ``TypeError`` instead of shifting values into the
wrong attributes. ``FunctionState``, ``ParamState`` and ``SchemaIssue`` are lite additions.

**Texts.** SurrealQL texts (``default``, ``value``, ``computed``, ``assertion``, ``when``, ``then``, ``view_query``,
permission rules, ``signup_fields`` values, ``signin_where``) are kept as the server rendered them — only the clause
keyword and the surrounding whitespace are removed — so they may differ between SurrealDB 2.x and 3.x. Every
definition the reader builds keeps the statement the server returned in ``raw`` (``""`` for a hand-built state).
``FieldState.default`` is SurrealQL text: ``default="18"``, ``default="'active'"``.

**repr.** A text may embed a credential (a literal password or token in an expression, a JWT key in an access
statement, a user's ``PASSHASH`` in an unmodelled section), and a logged exception or a debugger prints ``repr()``. So
``repr()`` — and ``str()`` — shows names and structure only: names, kinds, flags, numbers, durations and comments. Every
attribute holding SurrealQL text is left out of it (``raw``, ``default``, ``value``, ``assertion``, ``computed``,
``on_delete``, ``when``, ``then``, ``view_query``, ``signup``, ``signin``, ``signin_where``, a function's or a
param's ``permissions``, ``ParamState.value``, ``SchemaIssue.statement``), and a map of texts (``permissions``,
``signup_fields``, each section of ``other``) shows its keys only: ``permissions={'select': ..., 'update': ...}``,
``other={'users': {'admin': ...}}``. The attributes themselves hold the full texts.

::

    from surreal_orm_lite import FieldState, IndexState, SchemaState, TableState

    SchemaState()                                                # every attribute has a default
    TableState(name="person", relation_in="a | b").relation_in   # ('a', 'b')
    FieldState(name="age", field_type="int").nullable            # True (the full ORM's default)
    IndexState(name="by_email", fields=["email"]).fields         # ('email',)
    FieldState("age", "int", True, None, None, False)            # TypeError: the full ORM's 6th argument (encrypted)

A leaf module: it imports only ``dataclasses``, ``collections.abc`` and ``typing``.
"""

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import KW_ONLY, dataclass, field
from typing import Any, TypeVar

__all__ = [
    "AccessState",
    "AnalyzerState",
    "EventState",
    "FieldState",
    "FunctionState",
    "IndexState",
    "ParamState",
    "SchemaIssue",
    "SchemaState",
    "TableState",
]

_V = TypeVar("_V")


class _FrozenMap(Mapping[str, _V]):
    """A read-only mapping that iterates in sorted key order (codepoint order).

    Built from a mapping or from an iterable of ``(key, value)`` pairs (the last pair wins for a repeated key). It has
    no method that changes it, compares equal to any ``Mapping`` with the same items, is not hashable, shows as the
    sorted ``dict`` in ``repr`` and survives ``pickle``, ``copy.copy`` and ``copy.deepcopy`` as a ``_FrozenMap``.
    Built with ``hide_values=True`` — a map of SurrealQL texts: permission rules, expressions, the sections that are not
    modelled — it shows its keys only (``{'select': ..., 'update': ...}``) and keeps that through the copies; equality
    ignores it.
    """

    __slots__ = ("_hide_values", "_items")
    __hash__ = None  # type: ignore[assignment]  # equal to a dict, so unhashable like one

    def __init__(self, items: Mapping[str, _V] | Iterable[tuple[str, _V]] = (), hide_values: bool = False) -> None:
        pairs = items.items() if isinstance(items, Mapping) else items
        self._items: dict[str, _V] = dict(sorted(pairs, key=lambda pair: pair[0]))
        self._hide_values = hide_values

    def __getitem__(self, key: str) -> _V:
        return self._items[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, key: object) -> bool:
        return key in self._items

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Mapping):
            return self._items == dict(other.items())
        return NotImplemented

    def __repr__(self) -> str:
        if self._hide_values:
            return "{" + ", ".join(f"{key!r}: ..." for key in self._items) + "}"
        return repr(self._items)

    def __reduce__(self) -> tuple[type["_FrozenMap[_V]"], tuple[dict[str, _V], bool]]:
        return (_FrozenMap, (dict(self._items), self._hide_values))


def frozen_map(items: Mapping[str, _V] | Iterable[tuple[str, _V]]) -> _FrozenMap[_V]:
    """A read-only map of *items* (a mapping or an iterable of ``(key, value)`` pairs), sorted by key."""
    return _FrozenMap(items)


def _empty_map() -> _FrozenMap[Any]:
    """An empty read-only map: the ``default_factory`` of every mapping attribute."""
    return _FrozenMap()


def _freeze(value: Mapping[str, _V] | Iterable[tuple[str, _V]], hide_values: bool = False) -> _FrozenMap[_V]:
    """*value* as a read-only map, its values out of ``repr`` with *hide_values* (a ``_FrozenMap`` built the same way
    is kept as it is)."""
    if isinstance(value, _FrozenMap) and value._hide_values is hide_values:
        return value
    return _FrozenMap(value, hide_values)


def _freeze_texts(value: Mapping[str, str] | Iterable[tuple[str, str]]) -> _FrozenMap[str]:
    """*value* as a read-only map of SurrealQL texts: its keys show in ``repr``, its values do not."""
    return _freeze(value, hide_values=True)


def _freeze_sections(value: Mapping[str, Mapping[str, str]]) -> _FrozenMap[Mapping[str, str]]:
    """*value* as a read-only map of read-only maps of texts (``SchemaState.other``, ``TableState.other``): ``repr``
    shows the section names and the names in each section, never a definition."""
    return _FrozenMap((name, _freeze_texts(section)) for name, section in _freeze(value).items())


def _names(state: object, attribute: str, value: Iterable[str]) -> tuple[str, ...]:
    """*value* as a ``tuple`` of names; a ``str`` is refused (``TypeError``): it would split into
    characters."""
    if isinstance(value, str):
        hint = repr(value.split(".") if attribute == "path" else [value])
        raise TypeError(f"{type(state).__name__}.{attribute} takes a sequence of str, not a str: write {attribute}={hint}.")
    return tuple(value)


def _relation(value: Iterable[str] | str | None) -> tuple[str, ...]:
    """``relation_in``/``relation_out`` as a ``tuple``: the full ORM's pipe-joined ``str`` and ``None`` accepted."""
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(name.strip() for name in value.split("|") if name.strip())
    return tuple(value)


@dataclass(frozen=True, slots=True)
class FieldState:
    """One ``DEFINE FIELD`` of a table.

    The full ORM's attributes, positional and in its order: ``name``, ``field_type``, ``nullable``, ``default``,
    ``assertion``. Its 6th, ``encrypted``, is not provided (a hashing ``VALUE`` stays in ``value``), so every later
    attribute is keyword-only: ``flexible``, ``readonly``, ``value``, ``reference``, ``on_delete``, ``comment``
    (the full ORM's) and the lite additions ``path``, ``table``, ``default_always``, ``computed``, ``permissions``,
    ``raw``.

    ``name`` joins the path segments with ``.`` and marks array items with ``*`` (``"address.city"``, ``"tags.*"``,
    ``"first-name"``); ``path`` holds the unescaped segments and is derived from ``name`` when left empty — pass
    ``path=`` when a segment holds a dot (a backticked ``a.b``: ``path=["a.b"]``). ``field_type`` is the canonical
    kind without its optional wrapper (``"any"`` when there is no ``TYPE`` clause); ``nullable`` says the field may be
    ``NONE``. ``default``, ``value``, ``assertion`` and ``computed`` are SurrealQL text as the server rendered it
    (``default="18"``, ``default="'active'"``); a Python value is refused. Those texts, ``on_delete`` and ``raw`` are
    kept out of ``repr``; ``permissions`` shows its keys only.
    """

    name: str
    field_type: str
    nullable: bool = True
    default: str | None = field(default=None, repr=False)
    assertion: str | None = field(default=None, repr=False)
    _: KW_ONLY  # the full ORM's 6th attribute, encrypted, is not provided
    flexible: bool = False
    readonly: bool = False
    value: str | None = field(default=None, repr=False)
    reference: bool = False  # REFERENCE (SurrealDB 3.x)
    on_delete: str | None = field(default=None, repr=False)  # "IGNORE" | "UNSET" | "CASCADE" | "REJECT" | "THEN <expr>"
    comment: str | None = None
    path: Sequence[str] = ()  # unescaped segments ("tags", "*"); () → derived: tuple(name.split("."))
    table: str = ""
    default_always: bool = False
    computed: str | None = field(default=None, repr=False)  # COMPUTED expression text (SurrealDB 3.x)
    permissions: Mapping[str, str] = field(default_factory=_empty_map)  # select/create/update → rule
    raw: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        default: object = self.default
        if default is not None and not isinstance(default, str):
            raise TypeError(
                f"FieldState.default takes SurrealQL text (str) or None, not {type(default).__name__}: "
                "write default='18' for 18 or default=\"'active'\" for the string 'active'."
            )
        path = _names(self, "path", self.path)
        object.__setattr__(self, "path", path or tuple(self.name.split(".")))
        object.__setattr__(self, "permissions", _freeze_texts(self.permissions))


@dataclass(frozen=True, slots=True)
class IndexState:
    """One ``DEFINE INDEX`` of a table.

    The full ORM's attributes, positional and in its order: ``name``, ``fields``, ``unique``, ``search_analyzer``,
    ``bm25``, ``highlights``, ``hnsw``, ``dimension``, ``dist``, ``vector_type``, ``efc``, ``m``, ``concurrently``.
    Lite additions, keyword-only: ``table``, ``mtree``, ``count``, ``comment``, ``raw`` (kept out of ``repr``).

    ``fields`` holds canonical paths (the ``FieldState.name`` form), ``()`` for a ``COUNT`` index.
    ``search_analyzer`` is the analyzer of a ``SEARCH`` (2.x) or ``FULLTEXT`` (3.x) index; ``bm25`` is ``(k1, b)``
    for ``BM25(k1,b)`` and ``True`` for a bare ``BM25``.
    """

    name: str
    fields: Sequence[str] = ()
    unique: bool = False
    search_analyzer: str | None = None
    bm25: tuple[float, float] | bool | None = None
    highlights: bool = False
    hnsw: bool = False
    dimension: int | None = None
    dist: str | None = None  # "COSINE", "EUCLIDEAN", …
    vector_type: str | None = None  # "F32", "F64", …
    efc: int | None = None
    m: int | None = None
    concurrently: bool = False
    _: KW_ONLY
    table: str = ""
    mtree: bool = False  # SurrealDB 2.x
    count: bool = False  # COUNT index
    comment: str | None = None
    raw: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", _names(self, "fields", self.fields))
        bm25: object = self.bm25
        if isinstance(bm25, list):
            object.__setattr__(self, "bm25", tuple(bm25))


@dataclass(frozen=True, slots=True)
class EventState:
    """One ``DEFINE EVENT`` of a table.

    The full ORM's attributes, positional and in its order: ``name``, ``table``, ``when``, ``then``. Lite additions,
    keyword-only: ``comment``, ``async_mode``, ``retry``, ``max_depth``, ``raw``. ``when`` and ``then`` are the texts
    as the server rendered them (``"$event = 'CREATE'"``, ``"(CREATE log SET …)"``, a ``{ … }`` block); they and
    ``raw`` are kept out of ``repr``.

    ``async_mode`` is ``True`` for a SurrealDB 3.x ``ASYNC`` event (v0.31.0), run after the write's transaction
    commits; ``retry`` and ``max_depth`` are its ``RETRY`` and ``MAXDEPTH`` counts, ``None`` when the statement has
    none. SurrealDB 3.x renders both with every ``ASYNC`` (``ASYNC RETRY 1 MAXDEPTH 3`` by default), so an
    ``Event(..., async_mode=True)`` defined without them reads back ``retry=1, max_depth=3``.
    """

    name: str
    table: str
    when: str = field(repr=False)
    then: str = field(repr=False)
    _: KW_ONLY
    comment: str | None = None
    async_mode: bool = False  # SurrealDB 3.x
    retry: int | None = None
    max_depth: int | None = None
    raw: str = field(default="", repr=False)


@dataclass(frozen=True, slots=True)
class AccessState:
    """One database-level ``DEFINE ACCESS``.

    The full ORM's attributes, positional, in its order and with its defaults: ``name``, ``table``,
    ``signup_fields``, ``signin_where``, ``duration_token``, ``duration_session``, ``access_type``,
    ``duration_grant``. Lite additions, keyword-only: ``signup``, ``signin``, ``with_refresh``, ``comment``, ``raw``.

    ``table`` and ``signup_fields`` come from a ``SIGNUP (CREATE <table> SET <field> = <expr>, …)`` — every item,
    the ``id = $<key>`` of a model with a ``primary_key`` included —, ``signin_where`` from a ``SIGNIN (SELECT … FROM
    <table> WHERE <expr>)`` (``""``/empty otherwise). Durations are kept as rendered
    (``"15m"``, ``"NONE"``). The keys of a ``raw`` statement are already ``[REDACTED]`` by the server; ``raw``,
    ``signup``, ``signin`` and ``signin_where`` are kept out of ``repr`` all the same, and ``signup_fields`` shows its
    keys only.
    """

    name: str
    table: str = ""
    signup_fields: Mapping[str, str] = field(default_factory=_empty_map)
    signin_where: str = field(default="", repr=False)
    duration_token: str | None = "15m"
    duration_session: str | None = "12h"
    access_type: str = "RECORD"  # "RECORD" | "JWT" | "BEARER"
    duration_grant: str | None = None
    _: KW_ONLY
    signup: str | None = field(default=None, repr=False)  # the SIGNUP group, parentheses kept
    signin: str | None = field(default=None, repr=False)
    with_refresh: bool = False  # SurrealDB 3.x
    comment: str | None = None
    raw: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "signup_fields", _freeze_texts(self.signup_fields))


@dataclass(frozen=True, slots=True)
class AnalyzerState:
    """One ``DEFINE ANALYZER``.

    The full ORM's attributes, positional and in its order: ``name``, ``tokenizers``, ``filters``. Lite additions,
    keyword-only: ``function``, ``comment``, ``raw`` (kept out of ``repr``). Tokenizer and filter names are
    lower-cased (``("blank", "class")``, ``("lowercase", "snowball(english)")``); string arguments are kept as written.
    """

    name: str
    tokenizers: Sequence[str] = ()
    filters: Sequence[str] = ()
    _: KW_ONLY
    function: str | None = None  # "fn::x"
    comment: str | None = None
    raw: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "tokenizers", _names(self, "tokenizers", self.tokenizers))
        object.__setattr__(self, "filters", _names(self, "filters", self.filters))


@dataclass(frozen=True, slots=True)
class FunctionState:
    """One ``DEFINE FUNCTION`` (lite addition): ``name`` is ``"fn::…"``, ``returns`` the canonical kind of its
    ``-> kind`` (``None`` without one), ``permissions`` ``"FULL"``, ``"NONE"`` or ``"WHERE <expr>"``. ``permissions``
    and ``raw`` (the body) are kept out of ``repr``."""

    name: str
    parameters: Sequence[str]
    returns: str | None
    permissions: str = field(repr=False)
    comment: str | None
    raw: str = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", _names(self, "parameters", self.parameters))


@dataclass(frozen=True, slots=True)
class ParamState:
    """One ``DEFINE PARAM`` (lite addition): ``name`` without its ``$``. ``value`` and ``raw`` may hold a secret,
    so they are kept out of ``repr``, with ``permissions``."""

    name: str
    value: str = field(repr=False)
    permissions: str = field(repr=False)
    comment: str | None
    raw: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class SchemaIssue:
    """Something the reader could not turn into a state (lite addition), also logged once as a warning.

    ``kind`` is ``"table"``, ``"field"``, ``"index"``, ``"event"``, ``"access"``, ``"analyzer"``, ``"function"``,
    ``"param"`` or ``"section"``; ``name`` the key it was found under; ``message`` a fixed text that never holds a
    definition's text. ``statement`` holds the raw statement, when there is one, and is kept out of ``repr``.
    """

    kind: str
    name: str
    table: str | None
    message: str
    statement: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class TableState:
    """One ``DEFINE TABLE`` with its fields, indexes and events.

    The full ORM's attributes, positional and in its order: ``name``, ``schema_mode``, ``table_type``, ``fields``,
    ``indexes``, ``events``, ``changefeed``, ``permissions``. Its 9th, ``access``, is not provided (accesses are
    database-level: ``SchemaState.accesses``), so every later attribute is keyword-only: ``view_query``,
    ``relation_in``, ``relation_out``, ``enforced``, ``comment`` (the full ORM's) and the lite additions ``drop``,
    ``changefeed_include_original``, ``other``, ``raw``.

    ``schema_mode`` is ``"SCHEMAFULL"`` or ``"SCHEMALESS"``, ``table_type`` ``"normal"``, ``"relation"`` or
    ``"any"``. ``relation_in``/``relation_out`` are the names of ``TYPE RELATION IN a | b`` (``()`` for any table or
    not a relation); the full ORM's pipe-joined ``str`` is accepted. ``permissions`` maps ``select``/``create``/
    ``update``/``delete`` to ``"FULL"``, ``"NONE"`` or ``"WHERE <expr>"``; ``other`` holds the ``INFO FOR TABLE``
    sections that are not modelled. ``view_query`` and ``raw`` are kept out of ``repr``; ``permissions`` and each
    section of ``other`` show their keys only.
    """

    name: str
    schema_mode: str = "SCHEMAFULL"
    table_type: str = "normal"
    fields: Mapping[str, FieldState] = field(default_factory=_empty_map)  # key: FieldState.name
    indexes: Mapping[str, IndexState] = field(default_factory=_empty_map)
    events: Mapping[str, EventState] = field(default_factory=_empty_map)
    changefeed: str | None = None  # "1d", as rendered
    permissions: Mapping[str, str] = field(default_factory=_empty_map)
    _: KW_ONLY  # the full ORM's 9th attribute, access, is not provided (accesses are database-level)
    view_query: str | None = field(default=None, repr=False)  # "SELECT …" of AS SELECT, else None
    relation_in: Sequence[str] = ()
    relation_out: Sequence[str] = ()
    enforced: bool = False
    comment: str | None = None
    drop: bool = False
    changefeed_include_original: bool = False
    other: Mapping[str, Mapping[str, str]] = field(default_factory=_empty_map)
    raw: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", _freeze(self.fields))
        object.__setattr__(self, "indexes", _freeze(self.indexes))
        object.__setattr__(self, "events", _freeze(self.events))
        object.__setattr__(self, "permissions", _freeze_texts(self.permissions))
        object.__setattr__(self, "relation_in", _relation(self.relation_in))
        object.__setattr__(self, "relation_out", _relation(self.relation_out))
        object.__setattr__(self, "other", _freeze_sections(self.other))


@dataclass(frozen=True, slots=True)
class SchemaState:
    """The schema of one database, as ``DatabaseIntrospector.introspect()`` returns it.

    The full ORM's first attribute, ``tables``, is positional. Its 2nd, ``applied_migrations``, is not provided, so
    every later attribute is keyword-only: ``connection`` (``"default"`` or the name of the connection read),
    ``analyzers``, ``accesses`` (database-level), ``functions`` (key ``"fn::name"``), ``params`` (key without
    ``$``), ``other`` (the ``INFO FOR DB`` sections that are not modelled, as returned — the full ORM's ``apis`` are
    ``other["apis"]``) and ``issues`` (in the order found). Table names are unescaped. ``repr`` shows each section of
    ``other`` by its keys only (``other={'apis': {'/a': ...}}``), never a definition.
    """

    tables: Mapping[str, TableState] = field(default_factory=_empty_map)
    _: KW_ONLY  # the full ORM's 2nd attribute, applied_migrations, is not provided
    connection: str = "default"
    analyzers: Mapping[str, AnalyzerState] = field(default_factory=_empty_map)
    accesses: Mapping[str, AccessState] = field(default_factory=_empty_map)
    functions: Mapping[str, FunctionState] = field(default_factory=_empty_map)
    params: Mapping[str, ParamState] = field(default_factory=_empty_map)
    other: Mapping[str, Mapping[str, str]] = field(default_factory=_empty_map)
    issues: Sequence[SchemaIssue] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "tables", _freeze(self.tables))
        object.__setattr__(self, "analyzers", _freeze(self.analyzers))
        object.__setattr__(self, "accesses", _freeze(self.accesses))
        object.__setattr__(self, "functions", _freeze(self.functions))
        object.__setattr__(self, "params", _freeze(self.params))
        object.__setattr__(self, "other", _freeze_sections(self.other))
        object.__setattr__(self, "issues", tuple(self.issues))
