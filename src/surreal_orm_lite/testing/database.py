"""``embedded_database()``: an isolated in-process SurrealDB for one block (v0.25.0)."""

import contextlib
from collections.abc import AsyncIterator
from typing import Any

from ..connection_manager import SurrealDBConnectionManager
from ..embedded import require_supported_url, url_kind


@contextlib.asynccontextmanager
async def embedded_database(
    url: str = "mem://",
    *,
    namespace: str = "test",
    database: str = "test",
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

    The block changes the process-wide connection settings (every event loop and thread of the process uses
    the block's engine while it runs). Blocks nest: an inner block's database is separate from the outer one's,
    and the outer one is intact afterwards.

    :raises SurrealDbConnectionError: for a URL the installed SDK refuses (``rocksdb://`` today), quoting the
        SDK's message — before anything changes; or if the engine cannot be opened (the previous state is
        restored).
    :raises ValueError: for a server URL, or an empty namespace or database — before anything changes.
    """
    # Validated before anything changes, in this order: a URL the SDK refuses (E11) says what to fix
    # even when the namespace is empty too, and a server URL is named before an empty namespace.
    require_supported_url(url)
    if not url or url_kind(url) == "server":
        raise ValueError(f"embedded_database() needs an embedded URL (mem://, surrealkv://<path>, …), not {url!r}")
    if not namespace or not database:
        raise ValueError("embedded_database() needs a namespace and a database")

    state = await SurrealDBConnectionManager._enter_embedded_block(url, namespace, database)
    client: Any = None
    try:
        client = await SurrealDBConnectionManager.get_client()
        yield client
    finally:
        # This helper's own reference goes first: with the surrealdb SDK 2.x an in-process engine stops only
        # once nothing refers to its client, and the same surrealkv:// path may be reopened right after.
        del client
        await SurrealDBConnectionManager._exit_embedded_block(state)
