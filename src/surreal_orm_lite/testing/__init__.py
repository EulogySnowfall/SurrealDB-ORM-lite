"""Test helpers mirroring the full SurrealDB-ORM's ``surreal_orm.testing`` (v0.25.0, factories v0.40.0).

* :func:`embedded_database` — run a block against an isolated in-process SurrealDB (the official SDK's
  embedded engine: ``mem://`` by default, ``surrealkv://<path>`` on disk), then put the previous connection
  back. No server needed.
* :class:`SurrealFixture` and :func:`fixture` — declare model instances as test data; ``load()`` saves a copy
  of each for one block and deletes them afterwards (on any connection: a server or the embedded engine).
* :class:`ModelFactory` and :class:`Faker` — generate model instances from declared values and providers:
  ``build()`` needs no database, ``create()`` saves through the model's ``save()``.

Example::

    from surreal_orm_lite.testing import Faker, ModelFactory, SurrealFixture, embedded_database, fixture

    @fixture
    class UserFixtures(SurrealFixture):
        alice = User(name="Alice", role="admin")

    class UserFactory(ModelFactory[User]):
        class Meta:
            model = User

        name = Faker("name")
        role = "player"

    async with embedded_database():               # a fresh, empty in-memory database
        async with UserFixtures.load() as fixtures:
            assert fixtures.alice.get_id() is not None
            assert await User.objects().count() == 1
        await UserFactory.create_batch(3)         # saved through User.save()
        assert await User.objects().count() == 3

``ModelFactory`` and ``Faker`` (v0.40.0) declare test data as Factory Boy-style classes. Nothing here imports
pytest: the helpers work with any test runner (the opt-in pytest plugin is
``surreal_orm_lite.testing.pytest_plugin``, which this package does not import).
"""

from .database import embedded_database
from .factories import Faker, ModelFactory
from .fixtures import SurrealFixture, fixture

__all__ = ["Faker", "ModelFactory", "SurrealFixture", "embedded_database", "fixture"]
