"""Test helpers mirroring the full SurrealDB-ORM's ``surreal_orm.testing`` (v0.25.0).

* :func:`embedded_database` — run a block against an isolated in-process SurrealDB (the official SDK's
  embedded engine: ``mem://`` by default, ``surrealkv://<path>`` on disk), then put the previous connection
  back. No server needed.

Example::

    from surreal_orm_lite.testing import embedded_database

    async with embedded_database():               # a fresh, empty in-memory database
        await User(name="Ada").save()
        assert await User.objects().count() == 1

``ModelFactory`` comes in v0.39.0. Nothing here imports pytest: the helpers work with any test runner.
"""

from .database import embedded_database

__all__ = ["embedded_database"]
