"""Opt-in pytest plugin (v0.40.0): a fresh embedded SurrealDB per test, through ``surreal_db``.

Enable it in the root ``conftest.py``::

    pytest_plugins = ["surreal_orm_lite.testing.pytest_plugin"]

or on the command line with ``pytest -p surreal_orm_lite.testing.pytest_plugin``. It needs pytest and
pytest-asyncio (≥ 1.0); nothing in ``surreal_orm_lite`` imports this module, so the library never loads pytest
on its own. It registers two fixtures and nothing else (no hook, no marker, no option):

* ``surreal_db_url`` — the embedded URL of ``surreal_db``: ``"mem://"``. Override it in a conftest, e.g.
  ``return f"surrealkv://{tmp_path}/db"`` for an on-disk store. A server URL is refused by
  :func:`~surreal_orm_lite.testing.embedded_database` and reported as the test's setup error, so a test run
  cannot wipe a server's database through this fixture.
* ``surreal_db`` — a fresh, empty embedded database for the test: ``embedded_database(surreal_db_url)`` around
  it, on the current connection (``"default"``, or the ``using()`` block's), namespace and database ``test``; the
  previous settings come back after the test. Function scope; the event loop scope is pytest-asyncio's default
  (``asyncio_default_fixture_loop_scope``).

Example::

    @pytest.mark.asyncio                       # not needed with asyncio_mode = "auto"
    async def test_signup(surreal_db):
        await User(name="Ada").save()
        assert await User.objects().count() == 1

It works in pytest-asyncio **strict** mode (tests marked ``@pytest.mark.asyncio``) and **auto** mode.

``surreal_db`` yields ``None``, not the SDK client: pytest keeps a fixture's value until after its teardown, when
the block's engine is already closed. A test that needs raw SurrealQL calls
``await SurrealDBConnectionManager.get_client()``.

The engine is the optional extra ``surreal-orm-lite[embedded]`` (v0.50.0): without it, ``surreal_db`` reports
``SurrealDbConnectionError`` naming the extra as the test's setup error; loading the plugin never fails for it.
"""

from collections.abc import AsyncIterator

import pytest

try:
    import pytest_asyncio
except ImportError as error:
    raise ImportError("surreal_orm_lite.testing.pytest_plugin needs pytest-asyncio: pip install pytest-asyncio") from error

from .database import embedded_database


@pytest.fixture
def surreal_db_url() -> str:
    """The embedded URL of ``surreal_db``: ``"mem://"``. Override it in a conftest (e.g. ``f"surrealkv://{tmp_path}/db"``)."""
    return "mem://"


@pytest_asyncio.fixture
async def surreal_db(surreal_db_url: str) -> AsyncIterator[None]:
    """A fresh, empty embedded database for the test: ``embedded_database(surreal_db_url)`` around it; yields ``None``."""
    async with embedded_database(surreal_db_url):
        yield None
