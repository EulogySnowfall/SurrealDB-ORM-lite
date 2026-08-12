"""Static-typing regression guard — checked by mypy, never executed by pytest.

Nothing in ``src/`` declares a computed field, so a public API that only type-checks by
accident would pass ``mypy src/`` while breaking every downstream user on the first line of
the README. This module is the canonical user-side declaration, type-checked in CI.

Deliberately named ``typing_examples`` rather than ``test_*`` so pytest does not collect it:
the assertions here are made by mypy, not at runtime.
"""

from surreal_orm_lite import BaseSurrealModel, Computed, SurrealFunc, computed


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
