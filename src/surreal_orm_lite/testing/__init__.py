"""Test helpers mirroring the full SurrealDB-ORM's ``surreal_orm.testing`` (v0.25.0).

* :func:`embedded_database` — run a block against an isolated in-process SurrealDB (the official SDK's
  embedded engine: ``mem://`` by default, ``surrealkv://<path>`` on disk), then put the previous connection
  back. No server needed.
* :class:`SurrealFixture` and :func:`fixture` — declare model instances as test data; ``load()`` saves a copy
  of each for one block and deletes them afterwards (on any connection: a server or the embedded engine).

Example::

    from surreal_orm_lite.testing import SurrealFixture, embedded_database, fixture

    @fixture
    class UserFixtures(SurrealFixture):
        alice = User(name="Alice", role="admin")

    async with embedded_database():               # a fresh, empty in-memory database
        async with UserFixtures.load() as fixtures:
            assert fixtures.alice.get_id() is not None
            assert await User.objects().count() == 1

``ModelFactory`` comes in v0.40.0. Nothing here imports pytest: the helpers work with any test runner.
"""

from .database import embedded_database
from .fixtures import SurrealFixture, fixture

__all__ = ["SurrealFixture", "embedded_database", "fixture"]
