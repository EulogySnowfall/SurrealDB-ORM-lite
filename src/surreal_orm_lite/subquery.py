"""Subqueries (v0.27.0): a QuerySet used as a filter value in another QuerySet, compiled to a sub-SELECT.

Internal, except :class:`Subquery` (re-exported as ``surreal_orm_lite.Subquery``).

``Post.objects().filter(author__in=Subquery(active))`` — or ``author__in=active``: a QuerySet given as a filter
value is read as ``Subquery(qs)`` — sends **one** statement, inside which the server runs the inner queryset::

    { LET $_sq0 = (SELECT VALUE id FROM person WHERE active = $_f0); (SELECT * FROM post WHERE author IN $_sq0) };

- **A block, not a prelude.** The official SDK's ``query()`` returns the first statement's result only, so the full
  SurrealDB-ORM's two statements ``LET $_sq0 = (…); SELECT …;`` read ``None``. A block is one statement, whose value
  is its last one, on 2.x, 3.x and the embedded engine, over WebSocket and HTTP, in a buffered batch and in an
  interactive transaction. Its ``LET`` s are scoped to the block (a bound ``$_sq0`` is shadowed), each evaluated
  once, innermost first; the trailing statement is parenthesised, without which 2.x answers ``None``.
- **Once, in a ``LET``.** SurrealDB 3.2 and later give an inline uncorrelated sub-SELECT with ``ORDER BY`` and
  ``LIMIT`` its value for the first outer row only (surrealdb/surrealdb#7470). Bound once in a ``LET`` it is right
  on every line, and evaluated once instead of once per outer row.
- **``_sqv``.** A subquery ordered by a field compiles to ``(SELECT <column> AS _sqv, <order columns> FROM … ORDER
  BY …)._sqv``: 2.x refuses ``SELECT VALUE x … ORDER BY y`` and orders wrongly by a column it selects only under
  an alias, while this form is right on every line. ``_sqv`` (:data:`VALUE_ALIAS`) and ``_sq<N>``
  (:data:`LET_PREFIX`) are reserved names.
- **The first value.** ``exact``, ``gt``, ``gte``, ``lt``, ``lte``, ``contains`` and ``not_contains``
  (:data:`FIRST_VALUE_LOOKUPS`) compare with the subquery's first value, guarded:
  ``($_sq0 != [] AND x > array::first($_sq0))``. ``array::first([])`` is ``NONE``, and ``x > NONE`` would match
  every record. ``in``, ``not_in``, ``containsall`` and ``containsany`` (:data:`COLLECTION_LOOKUPS`) compare with
  all its values.
- **Live queries** keep the sub-SELECT inline (a ``LIVE SELECT`` cannot sit in a block), which is right there: the
  server evaluates it on every notification.
- **A subquery table that does not exist** reads as empty on 2.x and on the embedded engine. SurrealDB 3.x raises
  "The table '<t>' does not exist" (:func:`missing_table` reads the name); the ORM then sends the statement once
  more with every subquery of that table bound to ``[]`` (``LET $_sq0 = [];``), so a missing table reads as empty
  on every line, as the ORM reads any missing table.

:class:`SubqueryCompiler` is what one statement's subqueries share: the ``LET`` s, the tables they read, the
``variables()`` merged into the statement and the querysets being rendered.

``query_set`` and ``utils`` import this module, never the other way round at import time: ``QuerySet`` is
imported lazily, for ``isinstance``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from .query_set import QuerySet

#: The alias an ordered subquery selects its column under, read back with ``._sqv`` (reserved).
VALUE_ALIAS: Final = "_sqv"
#: The prefix of the block-scoped ``LET`` that binds each subquery: ``$_sq0``, ``$_sq1``… (reserved).
LET_PREFIX: Final = "_sq"
#: The lookups that compare a field with all the values of a subquery.
COLLECTION_LOOKUPS: Final = frozenset({"in", "not_in", "containsall", "containsany"})
#: The lookups that compare a field with the first value of a subquery (no record when it is empty).
FIRST_VALUE_LOOKUPS: Final = frozenset({"exact", "gt", "gte", "lt", "lte", "contains", "not_contains"})

_MISSING_TABLE: Final = re.compile(r"The table '([^']+)' does not exist")


class Subquery:
    """A QuerySet used as a filter value in another QuerySet, compiled to a sub-SELECT the server runs (v0.27.0).

    Same name and import as the full SurrealDB-ORM::

        from surreal_orm_lite import Subquery

        active = Person.objects().filter(active=True)
        posts = await Post.objects().filter(author__in=Subquery(active)).exec()
        posts = await Post.objects().filter(author__in=active).exec()           # the same: a QuerySet is a subquery
        oldest = Person.objects().order_by("-age").limit(1)
        theirs = await Post.objects().filter(author__in=Subquery(oldest)).count()
        best = Post.objects().select("score").order_by("-score").limit(1)
        top = await Post.objects().filter(score=Subquery(best)).exec()            # compared with its first value

    The inner queryset selects its record ids, or the one field given to ``select()``; ``filter()``, ``Q``,
    ``nearby()``, ``order_by()``, ``limit()``, ``offset()`` and ``variables()`` apply, and its ``fetch()`` is
    ignored (as in the full SurrealDB-ORM). It is read when the outer
    queryset runs (changing it afterwards changes the subquery), never on its own, and in the same statement: one
    request, one consistent read. ``in``, ``not_in``, ``containsall`` and ``containsany`` compare with all its
    values; ``exact``, ``gt``, ``gte``, ``lt``, ``lte``, ``contains`` and ``not_contains`` with its first value
    (order it to choose which), and match no record when it is empty.

    Args:
        queryset: the inner ``QuerySet`` (``Model.objects()…``).

    Raises:
        TypeError: ``queryset`` is not a ``QuerySet`` (S1).
    """

    __slots__ = ("queryset",)

    queryset: QuerySet

    def __init__(self, queryset: QuerySet) -> None:
        from .query_set import QuerySet

        if not isinstance(queryset, QuerySet):
            raise TypeError(not_a_queryset(queryset))
        self.queryset = queryset

    def __repr__(self) -> str:
        return f"Subquery({self.queryset!r})"

    def __eq__(self, other: object) -> bool:
        """Two subqueries are equal when they wrap the same queryset object."""
        if not isinstance(other, Subquery):
            return NotImplemented
        return self.queryset is other.queryset

    def __hash__(self) -> int:
        return id(self.queryset)


def subquery_of(value: object) -> Subquery | None:
    """The subquery a filter value stands for: the ``Subquery`` itself, ``Subquery(value)`` for a QuerySet, else ``None``."""
    if isinstance(value, Subquery):
        return value
    from .query_set import QuerySet

    if isinstance(value, QuerySet):
        return Subquery(value)
    return None


def missing_table(exc: BaseException) -> str | None:
    """The table a server error says does not exist (``"The table '<t>' does not exist"``), else ``None``.

    SurrealDB 3.x raises it for a table a statement reads before anyone wrote to it: ``NotFoundError``, or
    ``ThrownError`` ("An error occurred: …") for a ``LET`` inside a block on 3.1. The name only chooses which
    subquery to bind to ``[]``; it is never written into a statement.
    """
    match = _MISSING_TABLE.search(str(exc))
    return match.group(1) if match else None


def _same_value(a: object, b: object) -> bool:
    """Whether two ``variables()`` values bind the same value: the same object, or the same type and equal, recursively.

    Stricter than ``==`` on purpose: the server tells ``1`` from ``true`` (``true = 1`` is false), so ``1`` and
    ``True``, ``[1]`` and ``[True]``, ``{"k": 1}`` and ``{"k": True}`` differ here; so do ``1`` and ``1.0``, which the
    server would compare equal — never guess that two bindings coincide. An ``==`` that raises means "different".
    """
    if a is b:
        return True
    if type(a) is not type(b):
        return False
    try:
        if isinstance(a, list | tuple) and isinstance(b, list | tuple):
            return len(a) == len(b) and all(_same_value(x, y) for x, y in zip(a, b, strict=True))
        if isinstance(a, dict) and isinstance(b, dict):
            return a.keys() == b.keys() and all(_same_value(a[key], b[key]) for key in a)
        if isinstance(a, set | frozenset) and isinstance(b, set | frozenset):
            return {(type(x), x) for x in a} == {(type(x), x) for x in b}
        return bool(a == b)
    except Exception:
        return False


@dataclass(eq=False)
class SubqueryCompiler:
    """What the subqueries of one statement share, for one attempt at compiling it.

    Every field has a default, so ``SubqueryCompiler()`` stands on its own; ``QuerySet`` builds one per statement
    with the outer queryset's transaction, ``variables()`` and identity.

    Attributes:
        inline: the sub-SELECTs are written inline instead of bound in ``LET`` s (a live query).
        empty_tables: the tables whose subqueries are bound to ``[]`` (SurrealDB 3.x said they do not exist).
        outer_tx: the outer queryset's transaction; an inner queryset may share it, never have another.
        moment: the moment of an ``as_of()`` read, at which every subquery reads too (``None``: the current state).
        variables: the statement's ``variables()``: the outer queryset's, then each subquery's, merged.
        lets: the ``LET $_sq<N> = …;`` of the block, innermost first.
        tables: the tables the subqueries read.
        _stack: the ids of the querysets being rendered, the outer one first (a queryset cannot be its own subquery).
    """

    inline: bool = False
    empty_tables: frozenset[str] = frozenset()
    outer_tx: Any = None
    moment: datetime | None = None
    variables: dict[str, Any] = field(default_factory=dict)
    lets: list[str] = field(default_factory=list)
    tables: set[str] = field(default_factory=set)
    _stack: list[int] = field(default_factory=list)

    def merge_variables(self, variables: Mapping[str, Any]) -> None:
        """Add a subquery's ``variables()`` to the statement's, in order.

        A name already bound keeps its value when the new one is the same (:func:`_same_value`).

        Raises:
            ValueError: a name is already bound to another value (S7): the two querysets share one statement.
        """
        for name, value in variables.items():
            if name not in self.variables:
                self.variables[name] = value
            elif not _same_value(self.variables[name], value):
                raise ValueError(variable_clash(name))

    def reference(self, sub: Subquery, counter: int) -> tuple[str, dict[str, Any], int]:
        """Render *sub* for the statement; return what the condition names it by, its values and the next counter.

        The inner queryset is rendered by ``QuerySet._subquery_select()``, its filter values bound from *counter*
        on (one ``$_f<N>`` numbering for the whole statement). Inline (a live query), the sub-SELECT itself is
        returned; otherwise it is bound by a new ``LET $_sq<N> = …;`` and ``$_sq<N>`` returned. The subqueries of
        the inner queryset are rendered while it is, so their ``LET`` s come first: innermost first.

        Raises:
            ValueError: the inner queryset is being rendered already — the outer queryset itself, or a queryset
                that reaches itself through another subquery (S3). Checked before anything else about it.
        """
        queryset = sub.queryset
        if id(queryset) in self._stack:
            raise ValueError(own_subquery())
        self._stack.append(id(queryset))
        try:
            select, values, counter = queryset._subquery_select(self, counter)
        finally:
            self._stack.pop()
        if self.inline:
            return select, values, counter
        name = f"{LET_PREFIX}{len(self.lets)}"
        self.lets.append(f"LET ${name} = {select};")
        return f"${name}", values, counter

    def wrap(self, statement: str) -> str:
        """The statement to send: unchanged without subqueries, else the block ``{ LET …; (<statement>) };``."""
        if not self.lets:
            return statement
        return "{ " + " ".join(self.lets) + f" ({statement.removesuffix(';')}) }};"


class _MissingSubqueryTable(Exception):
    """SurrealDB 3.x said that *table*, which a ``LET`` of the statement reads, does not exist.

    Raised by ``QuerySet._execute_query()`` from the server's error and caught by ``QuerySet._run_compiled()``,
    which compiles the statement again with every subquery of that table bound to ``[]`` and sends it again: it
    never reaches the caller.
    """

    def __init__(self, table: str) -> None:
        super().__init__(table)
        self.table = table


def subquery_condition(
    field: str, lookup: str, op: str, sub: Subquery, counter: int, compiler: SubqueryCompiler | None
) -> tuple[str, dict[str, Any], int]:
    """The condition of a filter whose whole value is a subquery: ``(sql, values, next_counter)``.

    ``in``, ``not_in``, ``containsall`` and ``containsany`` (:data:`COLLECTION_LOOKUPS`) compare *field* with all
    the subquery's values (``author IN $_sq0``); ``exact``, ``gt``, ``gte``, ``lt``, ``lte``, ``contains`` and
    ``not_contains`` (:data:`FIRST_VALUE_LOOKUPS`) with its first value, guarded so an empty subquery matches no
    record: ``($_sq0 != [] AND score > array::first($_sq0))``.

    Args:
        field: the column, already validated and translated.
        lookup: the lookup name.
        op: its SurrealQL operator (``LOOKUP_OPERATORS``).
        sub: the subquery.
        counter: the next ``$_f<N>`` number.
        compiler: the statement's compiler; ``None`` for a ``Q`` compiled on its own.

    Raises:
        ValueError: any other lookup (S2); no compiler (S8); and what :meth:`SubqueryCompiler.reference` raises.
    """
    if lookup not in COLLECTION_LOOKUPS and lookup not in FIRST_VALUE_LOOKUPS:
        raise ValueError(lookup_refused(lookup))
    if compiler is None:
        raise ValueError(outside_a_queryset())
    ref, values, counter = compiler.reference(sub, counter)
    if lookup in COLLECTION_LOOKUPS:
        return f"{field} {op} {ref}", values, counter
    return f"({ref} != [] AND {field} {op} array::first({ref}))", values, counter


# --- the texts of the errors (spec §5.5) ----------------------------------------------------------


def not_a_queryset(value: object) -> str:
    """S1: ``Subquery()`` was given something other than a QuerySet."""
    return f"Subquery() takes a QuerySet (Model.objects()...), not {type(value).__name__}"


def lookup_refused(lookup: str) -> str:
    """S2: a lookup that can take no subquery (``isnull``, the text and the spatial lookups)."""
    return (
        f"'{lookup}' cannot take a subquery: in, not_in, containsall and containsany compare with all its values; "
        "exact, gt, gte, lt, lte, contains and not_contains with its first value"
    )


def own_subquery() -> str:
    """S3: a queryset is, directly or through another subquery, its own subquery."""
    return "A queryset cannot be its own subquery: build the inner queryset with a new Model.objects() call."


def clauses_refused(offenders: list[str]) -> str:
    """S4: the inner queryset has clauses a subquery cannot honour (*offenders*, sorted)."""
    return (
        f"A subquery cannot honour {', '.join(offenders)}: it gives the outer filter one list of values, SELECT VALUE "
        "<one field>. Drop the clause; filter(), Q, nearby(), select() of one field, order_by(), limit(), offset() "
        "and variables() are supported."
    )


def moment_refused() -> str:
    """S5: the inner queryset has an ``as_of()`` moment of its own."""
    return (
        "A subquery reads at the moment of the queryset that uses it: call as_of() on the outer queryset, not on the subquery."
    )


def transaction_refused() -> str:
    """S6: the inner queryset has a transaction that is not the outer queryset's."""
    return (
        "A subquery runs inside the statement of the queryset that uses it, so it cannot have a transaction of its "
        "own: build it with the same objects(tx=) as the outer queryset, or with none."
    )


def variable_clash(name: str) -> str:
    """S7: a subquery's ``variables()`` binds a name the statement already binds to another value."""
    return (
        f"The subquery binds ${name} to another value than the queryset that uses it; they share one statement: "
        "rename one of the variables()."
    )


def outside_a_queryset() -> str:
    """S8: a subquery in a ``Q`` (or ``build_filter_condition``) compiled with no statement to bind it in."""
    return (
        "A subquery compiles only inside a QuerySet (filter(), get_or_create(), live()...), which binds it in the "
        "statement it sends; Q.to_sql() alone cannot."
    )


def bad_slice(clause: str, value: object) -> str:
    """S12: the inner ``limit()`` or ``offset()`` (*clause*) is not an ``int >= 0``."""
    return f"A subquery's {clause} takes an int >= 0, got {value!r}"


def reserved_alias() -> str:
    """S13: the inner ``order_by()`` names ``_sqv`` (or a path under it) where the ordered form selects its alias."""
    return (
        f"'{VALUE_ALIAS}' names the values of an ordered subquery: a subquery can order by the column {VALUE_ALIAS} "
        f"only when it selects {VALUE_ALIAS} itself, and never by a path under it such as {VALUE_ALIAS}.x."
    )


def inside_a_list(field: str, lookup: str) -> str:
    """S15: a subquery given as an item of a list instead of as the whole value of a lookup."""
    return f"'{lookup}' takes a subquery as its whole value, not inside a list: {field}__{lookup}=Subquery(...)"
