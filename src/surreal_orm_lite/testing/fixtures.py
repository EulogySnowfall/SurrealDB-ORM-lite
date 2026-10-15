"""``SurrealFixture`` and ``@fixture``: declarative test data, saved for one block (v0.25.0).

The full SurrealDB-ORM's ``surreal_orm.testing`` names and shape: declare model instances as class attributes
of a :class:`SurrealFixture` subclass, decorate it with :func:`fixture`, and ``load()`` it as an async context
manager — every instance is saved (a fresh copy, in declaration order) for the block and deleted afterwards.

Example::

    from surreal_orm_lite.testing import SurrealFixture, fixture

    @fixture
    class UserFixtures(SurrealFixture):
        alice = User(name="Alice", role="admin")
        bob = User(id="bob", name="Bob", role="player")    # an explicit id is kept

    async with UserFixtures.load() as fixtures:
        assert fixtures.alice.get_id() is not None
        admins = await User.objects().filter(role="admin").exec()
    # alice and bob are deleted

Works on any configured connection: a server, or the in-process engine of
:func:`~surreal_orm_lite.testing.embedded_database`.
"""

import contextlib
import logging
from collections.abc import AsyncIterator, Iterable
from typing import ClassVar, Self, TypeVar

from ..model_base import BaseSurrealModel

logger = logging.getLogger("surreal_orm_lite.testing")

_F = TypeVar("_F", bound=type["SurrealFixture"])


def fixture(cls: _F) -> _F:
    """Collect the model instances declared on a SurrealFixture subclass, in declaration order.

    Only the class's own attributes are collected (as in the full ORM, inherited instances are not); every
    other attribute is ignored. The instances are templates: :meth:`SurrealFixture.load` saves copies of them.
    A subclass that declares instances of its own needs the decorator too; one that declares none loads its
    decorated parent's.

    ::

        @fixture
        class ProductFixtures(SurrealFixture):
            widget = Product(name="Widget", price=9.99)
            gadget = Product(name="Gadget", price=19.99)

    :raises TypeError: if ``cls`` is not a :class:`SurrealFixture` subclass.
    """
    if not (isinstance(cls, type) and issubclass(cls, SurrealFixture)):
        raise TypeError(f"@fixture decorates a SurrealFixture subclass, not {cls!r}")
    cls._fixture_instances = {name: value for name, value in vars(cls).items() if isinstance(value, BaseSurrealModel)}
    return cls


async def _delete_saved(saved: Iterable[BaseSurrealModel]) -> None:
    """Delete *saved* in reverse order, best effort: a failing delete is logged at DEBUG and skipped."""
    for instance in reversed(list(saved)):
        try:
            await instance.delete()
        except Exception:
            # The class and the record id only: the instance's fields may hold what a log must not show.
            logger.debug("Fixture cleanup: failed to delete %s %s", type(instance).__name__, instance.get_id(), exc_info=True)


class SurrealFixture:
    """Base class for declarative test fixtures: model instances declared as class attributes.

    Decorate the subclass with :func:`fixture`, then use :meth:`load` as an async context manager::

        @fixture
        class GameFixtures(SurrealFixture):
            table1 = GameTable(id="t1", name="Table 1", max_players=4)
            player1 = Player(name="Alice", seat=1)
            seated = SeatedAt(in_=RecordID("Player", "alice"), out=RecordID("GameTable", "t1"))

        async with GameFixtures.load() as fixtures:
            assert fixtures.table1.get_id() is not None   # saved in the database

    An edge (:class:`~surreal_orm_lite.BaseSurrealEdge`) is declared like any other instance, after its nodes
    (an explicit id makes a node referable).
    """

    _fixture_instances: ClassVar[dict[str, BaseSurrealModel]]

    @classmethod
    @contextlib.asynccontextmanager
    async def load(cls) -> AsyncIterator[Self]:
        """Save a fresh copy of every declared instance, yield them, delete them on exit.

        In declaration order, each template is copied (``model_copy(deep=True)``) and the copy saved; the
        yielded object holds the saved copies under the declared names (``fixtures.alice``), so each
        ``load()`` gets its own instances and the templates are never modified. On exit — normal, an
        exception or a cancellation — the copies are deleted in reverse order, best effort: a delete that
        fails (a record the block already removed) is logged at DEBUG and skipped.

        Two deliberate differences from the full SurrealDB-ORM: an ``id`` set on a template is **kept**
        (the full ORM resets it), so records and edges can refer to each other; and a load interrupted by a
        failing ``save()`` or a cancellation deletes the copies already saved before the exception
        propagates (the full ORM leaves them in the database).

        :raises ValueError: if the class declares no instance, or was not decorated with ``@fixture`` — including
            a subclass of a decorated fixture that declares instances of its own (only its parent's would load).
        """
        instances: dict[str, BaseSurrealModel] | None = getattr(cls, "_fixture_instances", None)
        own = vars(cls)
        if instances and "_fixture_instances" not in own and any(isinstance(value, BaseSurrealModel) for value in own.values()):
            # An undecorated subclass of a decorated fixture: it would load the inherited instances and silently
            # drop its own.
            raise ValueError(
                f"{cls.__name__} declares fixture instances but is not decorated with @fixture: only its parent's "
                f"would load. Did you forget the @fixture decorator?"
            )
        if not instances:
            raise ValueError(f"{cls.__name__} has no fixture instances. Did you forget the @fixture decorator?")

        holder = object.__new__(cls)
        saved: list[BaseSurrealModel] = []
        try:
            for name, template in instances.items():
                clone = template.model_copy(deep=True)
                # The template's id is kept (the full ORM resets it to None).
                await clone.save()
                saved.append(clone)
                setattr(holder, name, clone)
        except BaseException:
            # A failing save or a cancellation: the copies already saved are deleted before the exception
            # propagates (the full ORM leaves them in the database).
            await _delete_saved(saved)
            raise

        try:
            yield holder
        finally:
            await _delete_saved(saved)
