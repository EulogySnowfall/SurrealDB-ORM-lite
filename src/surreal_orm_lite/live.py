"""Live queries — the raw notification layer (v0.19.0).

A live query is a server-side subscription: SurrealDB pushes a notification over the
WebSocket every time a record in the watched table is created, updated or deleted. This
module turns that push stream into an async iterator of **raw envelopes**, and owns the two
pieces of plumbing the official SDK does not provide.

**Why the ORM does not call the SDK's ``subscribe_live()``.** The SDK's generator body is
``yield ret["result"]``: it hands back the record payload and discards the envelope around
it, so ``CREATE``, ``UPDATE`` and ``DELETE`` all arrive looking identical. The action is the
whole point of a live query, so the ORM registers its own queue in the connection's public
``live_queues`` registry — the same mechanism the SDK's own ``subscribe_live()`` uses — and
yields the complete envelope. An SDK build without that attribute is refused outright rather
than degraded to the SDK generator: action-less envelopes would silently break every
``notif["action"] == …`` comparison, and a wrong answer is worse than a clear failure.
``tests/test_live_queries.py`` guards the attribute, so an SDK bump that removes it fails in
CI rather than in production.

**Why the ORM owns end-of-stream.** A reader parked on ``queue.get()`` only ever wakes if
something wakes it, and the server cannot be relied on to do so. After ``kill()``, SurrealDB
3.x sends a final envelope with ``action="KILLED"`` and 2.6.x sends nothing at all; when the
WebSocket drops, the SDK's receive task swallows the close and never touches ``live_queues``,
so nothing arrives on either line. ``close_subscribers()`` (one live query) and
``close_all_subscribers()`` (connection teardown) push a private sentinel onto every queue
this module handed out, which makes termination identical everywhere. It has to be a separate
registry because the SDK's ``kill()`` pops its own ``live_queues`` entry, orphaning the queue
that was in it.

**What still hangs.** A WebSocket that drops on its own — no ``kill()``, no
``close_connection()`` — leaves readers suspended, because nothing in the SDK reports the
close to a live-query subscriber. Calling ``kill()`` or closing the connection through the
ORM always releases them. Automatic resubscribe is v0.21.0.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

from .enum import LiveAction
from .exceptions import SurrealDbError, SurrealDbNotFoundError

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import AsyncIterator

__all__ = ["LiveStream"]

# Private end-of-stream marker. Identity-compared, so it can never collide with a payload.
_STREAM_END: Final = object()

# Queues this module handed out, keyed by the event loop that owns them and then by the live
# query's uuid. Mirrors the SDK's ``live_queues`` so ``kill()`` can still reach a queue once the
# SDK has dropped its own entry.
#
# The loop is part of the key for the same reason the connection manager caches clients per loop
# (issue #163): an ``asyncio.Queue`` belongs to the loop that awaits it, and ``put_nowait()`` on a
# queue with a waiting getter schedules a callback on *that* loop. Waking another loop's queue
# from here would either raise or wake nothing; keying by loop makes it impossible to try.
_SUBSCRIBERS: dict[asyncio.AbstractEventLoop, dict[str, list[asyncio.Queue[Any]]]] = {}

# Uuids killed recently, per loop. A subscription opened on a dead uuid can never receive
# anything — no notification, and no sentinel, because ``kill()`` already fired — so it would
# wait forever. Remembering the last few lets ``open_stream`` hand back an empty iterator
# instead. Bounded, because this is a courtesy for a mis-sequenced call, not bookkeeping.
_RECENTLY_KILLED: dict[asyncio.AbstractEventLoop, deque[str]] = {}
_RECENTLY_KILLED_MAX = 64


def _key(query_uuid: str | UUID) -> str:
    return str(query_uuid)


def _prune_dead_loops() -> None:
    """Forget the registries of loops that have been closed, so they cannot accumulate."""
    for registry in (_SUBSCRIBERS, _RECENTLY_KILLED):
        for loop in [loop for loop in registry if loop.is_closed()]:
            registry.pop(loop, None)  # type: ignore[arg-type]


def _bucket() -> dict[str, list[asyncio.Queue[Any]]]:
    """This event loop's subscriber registry, created on first use."""
    _prune_dead_loops()
    return _SUBSCRIBERS.setdefault(asyncio.get_running_loop(), {})


def _killed() -> deque[str]:
    """This event loop's recently-killed uuids."""
    return _RECENTLY_KILLED.setdefault(asyncio.get_running_loop(), deque(maxlen=_RECENTLY_KILLED_MAX))


def _sdk_queues(client: Any) -> dict[str, list[asyncio.Queue[Any]]] | None:
    """The SDK connection's live-notification registry, or ``None`` if it has none.

    ``AsyncSurreal`` returns the connection directly today, but a session wrapper keeps the
    real connection on ``_connection``, so both shapes are tried before giving up.
    """
    for candidate in (client, getattr(client, "_connection", None)):
        queues = getattr(candidate, "live_queues", None)
        if isinstance(queues, dict):
            return queues
    return None


def register_subscriber(client: Any, query_uuid: str | UUID) -> asyncio.Queue[Any]:
    """Create a queue fed by ``query_uuid``'s notifications and register it everywhere."""
    key = _key(query_uuid)
    queue: asyncio.Queue[Any] = asyncio.Queue()
    queues = _sdk_queues(client)
    if queues is not None:
        queues.setdefault(key, []).append(queue)
    _bucket().setdefault(key, []).append(queue)
    return queue


def unregister_subscriber(client: Any, query_uuid: str | UUID, queue: asyncio.Queue[Any]) -> None:
    """Drop one subscriber, leaving any other reader of the same live query untouched."""
    key = _key(query_uuid)
    sdk = _sdk_queues(client)
    registries = [_bucket()] if sdk is None else [sdk, _bucket()]
    for registry in registries:
        holders = registry.get(key)
        if holders is None:
            continue
        if queue in holders:
            holders.remove(queue)
        if not holders:
            registry.pop(key, None)


def close_subscribers(query_uuid: str | UUID) -> None:
    """End every stream open on ``query_uuid``. Idempotent."""
    key = _key(query_uuid)
    for queue in _bucket().pop(key, []):
        queue.put_nowait(_STREAM_END)
    killed = _killed()
    if key not in killed:
        killed.append(key)


def close_all_subscribers() -> None:
    """End every stream open on this event loop.

    Called by the connection-teardown paths. A live query rides the WebSocket, so once the
    connection is gone no notification can ever arrive — and neither can the ``KILLED``
    envelope SurrealDB 3.x would otherwise send. Without this, closing the connection leaves
    every reader parked on ``queue.get()`` forever, and the ``async with`` around a
    :class:`LiveStream` never reaches its ``__aexit__``.
    """
    for key in list(_bucket()):
        close_subscribers(key)


def open_stream(client: Any, query_uuid: str | UUID) -> AsyncIterator[dict[str, Any]]:
    """Start buffering ``query_uuid``'s notifications now, and return an iterator over them.

    Deliberately **not** an ``async def`` generator. A generator body does not run until its
    first ``__anext__``, so a caller that starts a live query, writes a record and only then
    iterates would miss its own write. Registering the queue at call time closes that window:
    everything the server pushes from this moment on is buffered until the caller gets round
    to reading it.
    """
    queues = _sdk_queues(client)
    if queues is None:
        raise SurrealDbError(
            "This SurrealDB SDK connection does not expose a `live_queues` registry, which the "
            "ORM needs to read the `action` of each notification — the SDK's own "
            "`subscribe_live()` discards it. Live queries cannot be served correctly against "
            "this SDK build; pin a `surrealdb` release that provides it."
        )
    if _key(query_uuid) in _killed():
        return _exhausted()
    queue = register_subscriber(client, query_uuid)
    return _drain(client, query_uuid, queue)


async def _exhausted() -> AsyncIterator[dict[str, Any]]:
    """An already-finished stream, for a uuid this loop has already killed."""
    return
    yield {}  # pragma: no cover - unreachable, but makes this an async generator


async def _drain(client: Any, query_uuid: str | UUID, queue: asyncio.Queue[Any]) -> AsyncIterator[dict[str, Any]]:
    """Yield raw envelopes until the live query is killed, from either end-of-stream marker."""
    try:
        while True:
            envelope = await queue.get()
            if envelope is _STREAM_END:
                return
            if isinstance(envelope, dict) and envelope.get("action") == LiveAction.KILLED:
                return
            yield envelope
    finally:
        unregister_subscriber(client, query_uuid, queue)


def require_websocket(url: str | None) -> None:
    """Reject a live query on a transport that cannot carry one.

    The SDK's HTTP connection raises a bare ``NotImplementedError`` from ``live()``,
    ``subscribe_live()`` and ``kill()`` alike, which tells the caller nothing about why. The
    ORM checks the configured scheme first and says what to do instead.
    """
    if not (url or "").startswith(("ws://", "wss://")):
        raise SurrealDbError(
            f"Live queries require a WebSocket connection (ws:// or wss://); the configured URL "
            f"is {url!r}. Call SurrealDBConnectionManager.set_connection() with a ws:// URL."
        )


def missing_table_error(exc: Exception, table: str) -> SurrealDbNotFoundError:
    """Normalise SurrealDB 3.x's refusal to watch a table that does not exist.

    2.6.x accepts the subscription and simply never notifies, so this error only ever surfaces
    on 3.x. Subscribing is not a reason to create a table, so the ORM reports the difference
    rather than papering over it.
    """
    return SurrealDbNotFoundError(
        f"Cannot start a live query on {table!r}: the table does not exist. SurrealDB 3.x "
        f"refuses to watch an undefined table (2.6.x accepts it and stays silent). Create it "
        f"first, e.g. DEFINE TABLE {table} SCHEMALESS."
    )


class LiveStream:
    """Async context manager and iterator over one live query's raw notifications.

    Member names mirror the full ORM's ``LiveModelStream`` so the typed layer planned for
    v0.20.0 can present the same handle::

        async with User.objects().watch() as stream:
            async for notif in stream:
                print(notif["action"], notif["result"])
    """

    def __init__(self, table: str, starter: Any) -> None:
        self._table = table
        self._starter = starter
        self._live_id: UUID | None = None
        self._client: Any = None
        self._iterator: AsyncIterator[dict[str, Any]] | None = None

    @property
    def table(self) -> str:
        """The table this stream is subscribed to."""
        return self._table

    @property
    def live_id(self) -> UUID | None:
        """The live query's uuid, or ``None`` before start and after :meth:`stop`."""
        return self._live_id

    @property
    def is_active(self) -> bool:
        """Whether the subscription is currently running."""
        return self._live_id is not None

    async def start(self) -> LiveStream:
        """Open the subscription. Calling it on a running stream is a no-op."""
        if self._live_id is not None:
            return self
        from .connection_manager import SurrealDBConnectionManager

        # Client first, so no await sits between opening the subscription and buffering it.
        self._client = await SurrealDBConnectionManager.get_client()
        self._live_id = await self._starter()
        self._iterator = open_stream(self._client, self._live_id)
        return self

    async def stop(self) -> None:
        """Kill the subscription and end the iteration. Safe before start and to repeat."""
        if self._live_id is None:
            self._iterator = None
            return
        from .connection_manager import SurrealDBConnectionManager

        # `live_id` is cleared only once the kill has actually gone through. A transient
        # failure would otherwise leave the caller reporting `is_active is False` with a
        # subscription still running on the server and no uuid left to retry with.
        await SurrealDBConnectionManager.kill(self._live_id)
        self._live_id = None
        self._iterator = None

    def __aiter__(self) -> LiveStream:
        return self

    async def __anext__(self) -> dict[str, Any]:
        if self._iterator is None:
            raise StopAsyncIteration
        return await self._iterator.__anext__()

    async def __aenter__(self) -> LiveStream:
        return await self.start()

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.stop()
