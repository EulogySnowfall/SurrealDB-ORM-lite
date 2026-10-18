"""``embedded_database()``: an isolated in-process SurrealDB for one block (v0.25.0), on one connection (v0.29.0)."""

import contextlib
from collections.abc import AsyncIterator
from typing import Any

from ..connection_manager import SurrealDBConnectionManager, _unregister_block
from ..embedded import require_supported_url, url_kind
from ..routing import check_name


@contextlib.asynccontextmanager
async def embedded_database(
    url: str = "mem://",
    *,
    namespace: str = "test",
    database: str = "test",
    connection: str | None = None,
) -> AsyncIterator[Any]:
    """Run the block against an isolated in-process SurrealDB, then put the previous connection back.

    ::

        async with embedded_database() as client:      # a fresh, empty in-memory database
            await User(name="Ada").save()
            assert await User.objects().count() == 1
        # the in-memory database is gone; the previous settings, stored session and engine are back

    Yields the SDK client of the block's engine (for raw SurrealQL and DDL), valid **inside the block only**:
    with the surrealdb SDK 2.x, closing does not stop an in-process engine, so a client kept after the block
    is still a running database — do not use it afterwards, and drop it before reopening the same
    ``surrealkv://`` path. For an on-disk database pass ``surrealkv://<path>`` (with pytest, under
    ``tmp_path``): its files stay after the block.

    The block points **one connection** at its engine (v0.29.0): *connection*, else the current one when the block
    opens — the name of the :meth:`~surreal_orm_lite.SurrealDBConnectionManager.using` block around it, else
    ``"default"``. So inside ``using("archive")`` it isolates ``"archive"``, the connection every model of that block
    uses, and outside any ``using()`` block the default connection, as in v0.25.0. Only that connection's settings,
    stored session and engine are snapshotted and put back; the other connections are not touched. A name that is
    not registered is registered for the block (models routed to it work) and unregistered after it. A ``using()``
    block of another name entered inside this block reaches that connection's real database.

    The block changes that connection's settings for the whole process (every event loop and thread uses the
    block's engine while it runs). Blocks nest: an inner block's database is separate from the outer one's,
    and the outer one is intact afterwards.

    :param connection: the connection to isolate (v0.29.0); ``None`` (the default) for the current one.
    :raises SurrealDbConnectionError: for a URL the installed SDK refuses (``rocksdb://`` today), quoting the
        SDK's message — before anything changes; or if the engine cannot be opened, or another connection of the
        process has that on-disk store open (the previous state is restored).
    :raises ValueError: for a server URL, or an empty namespace or database; an empty *connection*; a *connection*
        other than the active ``using()`` block's, whose models would reach their real database — all before anything
        changes.
    :raises TypeError: *connection* is not a ``str`` — before anything changes.
    """
    # Validated before anything changes, in this order: a URL the SDK refuses (E11) says what to fix
    # even when the namespace is empty too, and a server URL is named before an empty namespace.
    require_supported_url(url)
    if not url or url_kind(url) == "server":
        raise ValueError(f"embedded_database() needs an embedded URL (mem://, surrealkv://<path>, …), not {url!r}")
    if not namespace or not database:
        raise ValueError("embedded_database() needs a namespace and a database")
    active = SurrealDBConnectionManager.get_active_connection_name()
    if connection is None:
        name = active or "default"
    else:
        name = check_name(connection)
        if active is not None and active != name:
            raise ValueError(
                f"embedded_database(connection={name!r}) cannot isolate {name!r} inside using({active!r}): every model "
                f"of the block would use {active!r} and reach its real database. Leave connection=None (it isolates "
                f"{active!r}), or open the block outside using()."
            )

    manager, created = SurrealDBConnectionManager._register_for_block(name)
    try:
        state = await manager._enter_embedded_block(url, namespace, database)
        client: Any = None
        try:
            client = await manager.get_client()
            yield client
        finally:
            # This helper's own reference goes first: with the surrealdb SDK 2.x an in-process engine stops only
            # once nothing refers to its client, and the same surrealkv:// path may be reopened right after.
            del client
            await manager._exit_embedded_block(state)
    finally:
        if created:
            _unregister_block(name, manager)
