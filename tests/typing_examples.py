"""Static-typing regression guard — checked by mypy, never executed by pytest.

Nothing in ``src/`` declares a computed field, so a public API that only type-checks by
accident would pass ``mypy src/`` while breaking every downstream user on the first line of
the README. This module is the canonical user-side declaration, type-checked in CI.

Deliberately named ``typing_examples`` rather than ``test_*`` so pytest does not collect it:
the assertions here are made by mypy, not at runtime.
"""

from surrealdb import RecordID

from surreal_orm_lite import BaseSurrealEdge, BaseSurrealModel, Computed, SurrealConfigDict, SurrealFunc, computed


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
