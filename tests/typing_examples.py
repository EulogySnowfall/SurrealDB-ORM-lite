"""Static-typing regression guard — checked by mypy, never executed by pytest.

Nothing in ``src/`` declares a computed field, so a public API that only type-checks by
accident would pass ``mypy src/`` while breaking every downstream user on the first line of
the README. This module is the canonical user-side declaration, type-checked in CI.

Deliberately named ``typing_examples`` rather than ``test_*`` so pytest does not collect it:
the assertions here are made by mypy, not at runtime.
"""

from datetime import timedelta
from typing import Any

from surrealdb import RecordID

from surreal_orm_lite import (
    BaseSurrealEdge,
    BaseSurrealModel,
    BoundExcluded,
    BoundIncluded,
    Computed,
    Duration,
    Range,
    SurrealConfigDict,
    SurrealFunc,
    Transaction,
    computed,
)


class Player(BaseSurrealModel):
    """The README §16 declaration, verbatim."""

    id: str
    first_name: str
    last_name: str
    full_name: Computed[str] = computed("string::concat(first_name, ' ', last_name)")
    name_len: Computed[int] = computed(SurrealFunc("string::len(full_name)"))


def read_computed(player: Player) -> str | None:
    """``Computed[str]`` must resolve to ``str | None``, not to an opaque marker type."""
    return player.full_name


def read_computed_int(player: Player) -> int | None:
    return player.name_len


class Tagged(BaseSurrealModel):
    """v0.22.0: ``table_name`` alone — every ``SurrealConfigDict`` key is optional (``total=False``)."""

    model_config = SurrealConfigDict(table_name="tagged")

    id: str | None = None


class Stamped(BaseSurrealModel):
    """The README §v0.18.0 declaration: ``server_fields`` alone used to fail mypy ("Missing keys")."""

    model_config = SurrealConfigDict(server_fields=["created"])

    id: str | None = None
    created: str | None = None


def tagged_table() -> str:
    return Tagged.get_table_name()


class Purchased(BaseSurrealEdge):
    """v0.22.0: an edge model. ``in_`` (column ``in``) and ``out`` are declared by the base."""

    model_config = SurrealConfigDict(table_name="purchased")

    quantity: int = 1
    price: float = 0.0


def new_purchase() -> Purchased:
    """The endpoints are ``RecordID`` parameters named ``in_`` and ``out`` — not the keyword ``in``."""
    return Purchased(in_=RecordID("Person", "a"), out=RecordID("Product", "w"), quantity=2)


def purchase_buyer(edge: Purchased) -> RecordID:
    """An endpoint reads back as a ``RecordID``, whatever form it was given in."""
    return edge.in_


def purchase_item(edge: Purchased) -> RecordID:
    return edge.out


class Person(BaseSurrealModel):
    id: str | None = None


class Product(BaseSurrealModel):
    id: str | None = None


async def typed_relate(a: Person, p: Product) -> Purchased:
    """An edge class gives back that edge, typed (the first ``relate()`` overload)."""
    return await a.relate(Purchased, p, data={"quantity": 1})


async def typed_relate_in_tx(a: Person, p: Product, tx: Transaction) -> Purchased:
    """``tx`` by position, the full ORM's order: ``(edge, target, reverse, tx)``."""
    return await a.relate(Purchased, p, False, tx)


async def raw_relate(a: Person, b: Person) -> list[dict[str, Any]]:
    """An edge table name keeps the v0.6.0 return: the ``RELATE`` rows."""
    return await a.relate("follows", b)


async def related_through_edge_class(a: Person) -> list[Any]:
    return await a.get_related(Purchased, model_class=Product)


async def typed_reverse(a: Person, p: Product) -> Purchased:
    """``reverse=True`` and keyword edge fields keep the edge-class overload."""
    return await a.relate(Purchased, p, reverse=True, quantity=1)


async def typed_reverse_positional(a: Person, p: Product) -> Purchased:
    """A positional ``reverse`` (the full ORM's order) is still typed ``Purchased``."""
    return await a.relate(Purchased, p, True, None, quantity=1)


async def raw_kwargs(a: Person, b: Person) -> list[dict[str, Any]]:
    """The string form with keyword edge fields keeps the ``RELATE`` rows."""
    return await a.relate("follows", b, reverse=True, since="2025-01-01")


async def unlink(a: Person, p: Product) -> None:
    """``reverse=``/``tx=`` on ``remove_relation``, a bare id, and a mixed list of edges."""
    await a.remove_relation(Purchased, p, reverse=True, tx=None)
    await a.remove_relation("follows", "abc123")
    await a.remove_all_relations([Purchased, "follows"], direction="both", tx=None)
    await a.remove_all_relations([Purchased, "follows"], "both", None)


async def both(a: Person) -> list[Any]:
    return await a.get_related("follows", direction="both")


async def related_positional(a: Person) -> list[Any]:
    """``get_related(edge, direction, model_class)`` by position, the full ORM's order."""
    return await a.get_related("follows", "in", Person)


class Timed(BaseSurrealModel):
    """v0.23.0: a ``Duration`` field keeps the server's nanoseconds."""

    ttl: Duration | None = None


def make_duration() -> Duration:
    """``parse`` is a classmethod returning ``Self``: typed ``Duration``, not the SDK's class."""
    return Duration.parse("1h30m")


def from_seconds() -> Duration:
    """The SDK's signature: an ``int`` of seconds, plus nanoseconds."""
    return Duration.parse(90, 5)


def compact(d: Duration) -> list[int]:
    return d.to_compact()


def as_td(d: Duration) -> timedelta:
    return d.to_timedelta()


class Shift(BaseSurrealModel):
    """v0.23.0: a ``Range[int]`` field validates its bounds as ``int``."""

    hours: Range[int] | None = None


def nine_to_five() -> Range[int]:
    """``Range`` is generic: ``Range[int](...)`` is typed ``Range[int]``."""
    return Range[int](BoundIncluded(9), BoundExcluded(17))


def open_end(start: int) -> Range[int]:
    """An open side is ``None``; the bound attributes are optional."""
    shift = Range[int](BoundIncluded(start), None)
    begin: BoundIncluded | BoundExcluded | None = shift.begin
    assert begin is not None
    return shift
