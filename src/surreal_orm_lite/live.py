"""Live queries — the raw notification layer (v0.19.0) and the typed layer on top (v0.20.0).

A live query is a server-side subscription: SurrealDB pushes a notification over the
WebSocket every time a record in the watched table is created, updated or deleted. This
module turns that push stream into an async iterator of **raw envelopes**, and owns the two
pieces of plumbing the official SDK does not provide. :class:`LiveModelStream` then converts
each envelope into a :class:`ModelChangeEvent` carrying a model instance — the full
SurrealDB-ORM's API, so code written against it migrates with an import change.

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

**Dropped connections (v0.21.0).** A WebSocket that drops on its own — no ``kill()``, no
``close_connection()`` — used to leave readers suspended, because nothing in the SDK reports the
close to a live-query subscriber. Its receive task does end, though, within milliseconds on both
server lines, and every reader now watches it. A stream with ``auto_resubscribe`` (the default)
reconnects and moves its buffer onto a new live query; any other reader raises
``SurrealDbConnectionError``. Deliberate teardowns release their readers *before* the socket
closes, which is how the two cases are told apart. What changed while the connection was down is
not replayed: a live query only sees what happens while it runs.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import warnings
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine, Generator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Generic, TypeVar, cast
from uuid import UUID

from pydantic import ValidationError

from .enum import LiveAction
from .exceptions import (
    SurrealDbAuthenticationError,
    SurrealDbConnectionError,
    SurrealDbError,
    SurrealDbNotFoundError,
)
from .utils import user_stacklevel

if TYPE_CHECKING:
    from .model_base import BaseSurrealModel

__all__ = ["LiveIterator", "LiveModelStream", "LiveStream", "ModelChangeEvent", "ReconnectCallback"]

logger = logging.getLogger(__name__)

T = TypeVar("T", bound="BaseSurrealModel")

#: ``on_reconnect(old_id, new_id)`` — called once a dropped live query is running again under
#: ``new_id``. Sync or async. The full ORM passes ``str`` ids; lite passes the ``UUID`` its
#: ``live_id`` already is, so ``str(old_id)`` code works with both.
ReconnectCallback = Callable[[UUID, UUID], Awaitable[None] | None]

# Private end-of-stream marker. Identity-compared, so it can never collide with a payload.
_STREAM_END: Final = object()


class _Lost:
    """Enqueued when the subscription is gone for good: the reader raises :attr:`error`.

    A class rather than a second sentinel, because the reader has to say *why* — a dropped
    connection, or the reconnect that gave up and the error that made it give up.
    """

    __slots__ = ("error",)

    def __init__(self, error: Exception) -> None:
        self.error = error


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
    """Forget the registries of loops that have been closed, so they cannot accumulate.

    Iterates a snapshot: with a loop per thread (issue #163), another thread's ``_bucket()`` can
    add its loop to the same dict while ``is_closed()`` runs, and iterating the live dict would
    then raise ``dictionary changed size during iteration``.
    """
    for registry in (_SUBSCRIBERS, _RECENTLY_KILLED):
        for loop in list(registry):
            if loop.is_closed():
                registry.pop(loop, None)  # type: ignore[arg-type]


def _bucket() -> dict[str, list[asyncio.Queue[Any]]]:
    """This event loop's subscriber registry, created on first use."""
    _prune_dead_loops()
    return _SUBSCRIBERS.setdefault(asyncio.get_running_loop(), {})


def _killed_on(loop: asyncio.AbstractEventLoop) -> deque[str]:
    return _RECENTLY_KILLED.setdefault(loop, deque(maxlen=_RECENTLY_KILLED_MAX))


def _killed() -> deque[str]:
    """This event loop's recently-killed uuids."""
    return _killed_on(asyncio.get_running_loop())


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


def recv_task_of(client: Any) -> asyncio.Task[Any] | None:
    """The SDK WebSocket connection's receive task, or ``None`` (HTTP, or not connected yet).

    The task reads the socket for as long as it is open, and **ends when the connection drops**
    — measured on 2.7.0 and 3.3.2, within milliseconds of an abort. It is the only drop signal
    the SDK exposes: the receive loop swallows the close and tells no one else.
    """
    for candidate in (client, getattr(client, "_connection", None)):
        task = getattr(candidate, "recv_task", None)
        if isinstance(task, asyncio.Task):
            return task
    return None


def is_dead(client: Any) -> bool:
    """Whether *client*'s WebSocket has dropped. An HTTP client never is: it holds no socket."""
    task = recv_task_of(client)
    return task is not None and task.done()


def register_subscriber(client: Any, query_uuid: str | UUID, queue: asyncio.Queue[Any] | None = None) -> asyncio.Queue[Any]:
    """Register a queue fed by ``query_uuid``'s notifications everywhere — a new one by default.

    Passing an existing *queue* is how a resubscribe keeps a reader's buffer: what it has not
    read yet stays queued, and a read parked on it simply completes with the new uuid's events.
    """
    key = _key(query_uuid)
    if queue is None:
        queue = asyncio.Queue()
    queues = _sdk_queues(client)
    if queues is not None:
        queues.setdefault(key, []).append(queue)
    _bucket().setdefault(key, []).append(queue)
    return queue


def _is_registered(loop: asyncio.AbstractEventLoop, query_uuid: str | UUID, queue: asyncio.Queue[Any]) -> bool:
    """Whether *queue* still reads *query_uuid* on *loop* — i.e. nobody released it on purpose."""
    return queue in _SUBSCRIBERS.get(loop, {}).get(_key(query_uuid), ())


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


def release_subscribers(query_uuid: str | UUID) -> None:
    """Wake every reader of ``query_uuid`` on this loop with end-of-stream. Idempotent.

    Deliberately separate from :func:`mark_killed`: a ``kill()`` that *fails* must still release
    its readers — nothing else would wake them — but must not record the uuid as dead, because
    the subscription is still running on the server and a retry has to be able to re-attach.
    """
    for queue in _bucket().pop(_key(query_uuid), []):
        queue.put_nowait(_STREAM_END)


def mark_killed(query_uuid: str | UUID) -> None:
    """Record ``query_uuid`` as dead on this loop, so a later subscription ends at once."""
    key = _key(query_uuid)
    killed = _killed()
    if key not in killed:
        killed.append(key)


def close_subscribers(query_uuid: str | UUID) -> None:
    """End every stream open on ``query_uuid`` and record the uuid as dead. Idempotent."""
    release_subscribers(query_uuid)
    mark_killed(query_uuid)


def _release_loop(loop: asyncio.AbstractEventLoop) -> None:
    """End every stream registered on *loop*. Must run **on** that loop."""
    killed = _killed_on(loop)
    for key, queues in _SUBSCRIBERS.pop(loop, {}).items():
        for queue in queues:
            queue.put_nowait(_STREAM_END)
        if key not in killed:
            killed.append(key)


def close_all_subscribers(*, every_loop: bool = False) -> None:
    """End every stream open on this event loop — and, with ``every_loop``, on all of them.

    Called by the connection-teardown paths. A live query rides the WebSocket, so once the
    connection is gone no notification can ever arrive — and neither can the ``KILLED``
    envelope SurrealDB 3.x would otherwise send. Without this, closing the connection leaves
    every reader parked on ``queue.get()`` forever, and the ``async with`` around a
    :class:`LiveStream` never reaches its ``__aexit__``.

    ``every_loop`` serves ``close_all_connections()``, which drops every loop's client. Another
    loop's queues can only be woken from that loop's own thread, so the release is handed to it
    with ``call_soon_threadsafe`` rather than performed here.
    """
    current = asyncio.get_running_loop()
    _prune_dead_loops()
    _release_loop(current)
    if not every_loop:
        return
    for loop in list(_SUBSCRIBERS):
        if loop is current or loop.is_closed():
            continue
        try:
            loop.call_soon_threadsafe(_release_loop, loop)
        except RuntimeError:  # pragma: no cover - closed between the check and the call
            _SUBSCRIBERS.pop(loop, None)


class LiveIterator:
    """Async iterator over one reader's raw notification envelopes.

    A class rather than an ``async def`` generator, for two reasons a generator cannot meet:

    * **Registration happens at construction.** A generator body does not run until its first
      ``__anext__``, so a caller that subscribes, writes a record and only then iterates would
      miss its own write.
    * **A cancelled read loses nothing.** Cancelling a generator's pending ``__anext__`` throws
      into its body, runs its ``finally`` and finishes it for good — so the ordinary polling
      idiom ``asyncio.wait_for(anext(stream), timeout)`` would end the stream on its first quiet
      tick. Here the pending ``queue.get()`` is simply abandoned; ``asyncio.Queue`` removes no
      item for a cancelled getter, and the next read picks up where the last one left off.

    The iteration ends when the live query is killed or its connection is closed. If the
    WebSocket **drops** instead, it raises :class:`SurrealDbConnectionError` — the subscription
    died with the socket, and waiting on would mean waiting forever. Call :meth:`aclose` to stop
    reading early without killing the live query.

    **Telling a drop from a teardown.** Both end the SDK's receive task, which is the only drop
    signal there is. Every deliberate path — ``kill()``, ``close_connection()``, the connection
    manager's other teardowns — releases the reader's queue *before* the socket goes, so a queue
    still registered when the task ends means nobody asked for it: that is a drop.
    ``on_lost`` lets :class:`LiveStream` take over at that point and resubscribe.
    """

    def __init__(
        self,
        client: Any,
        query_uuid: str | UUID,
        queue: asyncio.Queue[Any] | None,
        *,
        on_lost: Callable[[], None] | None = None,
    ) -> None:
        self._client = client
        self._query_uuid = query_uuid
        self._queue = queue
        self._on_lost = on_lost
        self._loop: asyncio.AbstractEventLoop | None = None
        self._watched: asyncio.Task[Any] | None = None
        if queue is not None:
            self._loop = asyncio.get_running_loop()
            self._watch(client)

    def _watch(self, client: Any) -> None:
        task = recv_task_of(client)
        if task is not None:
            self._watched = task
            # On a task that is already done, the callback is scheduled at once: a drop that
            # happened before the reader existed is still reported.
            task.add_done_callback(self._connection_lost)

    def _unwatch(self) -> None:
        task, self._watched = self._watched, None
        if task is not None:
            task.remove_done_callback(self._connection_lost)

    def _connection_lost(self, _task: asyncio.Task[Any]) -> None:
        """The SDK's receive task ended: the socket is gone, whether or not anyone asked."""
        queue = self._queue
        if queue is None or self._loop is None or not _is_registered(self._loop, self._query_uuid, queue):
            return  # released on purpose — kill() or a connection teardown got there first
        if self._on_lost is not None:
            self._on_lost()
            return
        queue.put_nowait(
            _Lost(
                SurrealDbConnectionError(
                    f"The WebSocket connection carrying live query {self._query_uuid} dropped; "
                    "the server removed the subscription with it. Start a new one, or use "
                    "watch()/live() with auto_resubscribe=True to have the ORM do it."
                )
            )
        )

    def __aiter__(self) -> LiveIterator:
        return self

    async def __anext__(self) -> dict[str, Any]:
        if self._queue is None:
            raise StopAsyncIteration
        envelope = await self._queue.get()
        if isinstance(envelope, _Lost):
            self._detach()
            raise envelope.error
        if envelope is _STREAM_END or (isinstance(envelope, dict) and envelope.get("action") == LiveAction.KILLED):
            self._detach()
            raise StopAsyncIteration
        return envelope  # type: ignore[no-any-return]

    async def aclose(self) -> None:
        """Stop reading and drop this reader's buffer. The live query itself keeps running."""
        self._detach()

    def rebind(self, client: Any, query_uuid: str | UUID) -> None:
        """Move this reader, buffer and all, onto a resubscribed live query on *client*."""
        queue = self._queue
        if queue is None:
            return
        self._unwatch()
        unregister_subscriber(self._client, self._query_uuid, queue)
        self._client, self._query_uuid = client, query_uuid
        register_subscriber(client, query_uuid, queue)
        self._watch(client)

    def fail(self, error: Exception) -> None:
        """End the iteration with *error*, after whatever is still buffered."""
        if self._queue is not None:
            self._queue.put_nowait(_Lost(error))

    def _detach(self) -> None:
        self._unwatch()
        if self._queue is None:
            return
        queue, self._queue = self._queue, None
        unregister_subscriber(self._client, self._query_uuid, queue)


def open_stream(client: Any, query_uuid: str | UUID, *, on_lost: Callable[[], None] | None = None) -> LiveIterator:
    """Start buffering ``query_uuid``'s notifications now, and return an iterator over them.

    Everything the server pushes from this moment on is buffered until the caller gets round to
    reading it — see :class:`LiveIterator` for why registration cannot wait for the first read.
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
        return LiveIterator(client, query_uuid, None)
    return LiveIterator(client, query_uuid, register_subscriber(client, query_uuid), on_lost=on_lost)


def is_connection_failure(exc: BaseException, client: Any) -> bool:
    """Whether a failed resubscribe attempt is worth retrying: was the *connection* the problem?

    Retried: the ORM's own connection error (server unreachable), an ``OSError``, anything the
    ``websockets`` transport raises (matched by module, so the ORM imports nothing from it), and
    anything at all raised on a client that is dead by now — the SDK reports a request caught by
    a drop as a bare ``KeyError`` (its receive loop clears the pending-request map before the
    request's own cleanup runs). Everything else — an authentication failure, a removed table —
    would fail identically on the next attempt, so it ends the stream instead. That is also what
    keeps a rejected session token from being "retried" into a subscription opened as the
    configured user.
    """
    if isinstance(exc, (SurrealDbConnectionError, OSError)):
        return True
    if type(exc).__module__.split(".", 1)[0] == "websockets":
        return True
    return client is not None and is_dead(client)


def cancelled_from_outside() -> bool:
    """Whether the running task is being cancelled, as opposed to seeing an SDK future cancelled."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


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
        f"first, e.g. DEFINE TABLE {table} SCHEMALESS. Server said: {exc}"
    )


class LiveStream:
    """Async context manager and iterator over one live query's raw notifications.

    Member names mirror the full ORM's ``LiveModelStream``, which :class:`LiveModelStream`
    wraps this class to provide::

        async with User.objects().watch() as stream:
            async for notif in stream:
                print(notif["action"], notif["result"])

    **Surviving a dropped connection (v0.21.0).** With ``auto_resubscribe`` (the default) a
    WebSocket that drops — not one closed through the ORM — is answered by a background task:
    it opens a new connection (``get_client()``, which signs in and replays the session), sends
    the same ``LIVE SELECT`` again, and moves this stream's buffer onto the new uuid, so a read
    parked in ``async for`` simply completes later. ``live_id`` changes; ``on_reconnect(old_id,
    new_id)`` is then called. Only connection failures are retried, with exponential backoff
    (:attr:`reconnect_delay` doubling up to :attr:`reconnect_max_delay`, at most
    :attr:`reconnect_max_attempts` tries); any other failure, or the last attempt failing, is
    raised from the iteration. **What changed during the outage is not replayed** — a live query
    only reports what happens while it runs — so ``on_reconnect`` is where to catch up.
    """

    #: Seconds before the first reconnect attempt after a drop. Doubles after each failure.
    reconnect_delay: float = 0.5
    #: Upper bound of the delay between two reconnect attempts, in seconds.
    reconnect_max_delay: float = 30.0
    #: Attempts before giving up and raising from the iteration; ``None`` retries forever.
    reconnect_max_attempts: int | None = 10

    def __init__(
        self,
        table: str,
        starter: Callable[..., Awaitable[UUID]],
        *,
        auto_resubscribe: bool = True,
        on_reconnect: ReconnectCallback | None = None,
    ) -> None:
        self._table = table
        self._starter = starter
        self._auto_resubscribe = auto_resubscribe
        self._on_reconnect = on_reconnect
        self._live_id: UUID | None = None
        self._client: Any = None
        self._iterator: LiveIterator | None = None
        # Where the reconnect policy is read from: this stream, or the typed stream wrapping it,
        # so that ``LiveModelStream.reconnect_max_attempts = …`` is honoured too.
        self._policy: Any = self
        self._stopping = False
        self._resubscribe_pending = False
        self._tasks: set[asyncio.Task[None]] = set()
        # The connection manager's identity when the live query was opened (see _identity_refusal).
        self._identity: tuple[int, bool] | None = None

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

        # Transport first: on an http:// URL no client should be opened just to be refused.
        require_websocket(SurrealDBConnectionManager.get_connection_string())
        # Client next, so no await sits between opening the subscription and buffering it.
        self._stopping = False
        self._client = await SurrealDBConnectionManager.get_client()
        self._identity = SurrealDBConnectionManager._identity_snapshot()
        self._live_id = await self._starter(self._client)
        on_lost = self._schedule_resubscribe if self._auto_resubscribe else None
        self._iterator = open_stream(self._client, self._live_id, on_lost=on_lost)
        return self

    async def stop(self) -> None:
        """Kill the subscription and end the iteration. Safe before start and to repeat.

        A resubscribe still in progress is cancelled first, and a subscription it managed to
        open meanwhile is killed too.
        """
        self._stopping = True
        await self._cancel_tasks()
        if self._live_id is None:
            await self._drop_iterator()
            return
        from .connection_manager import SurrealDBConnectionManager

        # `live_id` is cleared only once the kill has actually gone through. A transient
        # failure would otherwise leave the caller reporting `is_active is False` with a
        # subscription still running on the server and no uuid left to retry with.
        await SurrealDBConnectionManager.kill(self._live_id)
        self._live_id = None
        await self._drop_iterator()

    async def _drop_iterator(self) -> None:
        iterator, self._iterator = self._iterator, None
        if iterator is not None:
            await iterator.aclose()

    # -- resubscribe (v0.21.0) ---------------------------------------------------------------

    def _schedule_resubscribe(self) -> None:
        """The reader's socket dropped: start resubscribing, unless already on it or stopping."""
        if self._stopping or self._resubscribe_pending or self._live_id is None:
            return
        self._resubscribe_pending = True
        task = asyncio.get_running_loop().create_task(self._resubscribe(self._live_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _cancel_tasks(self) -> None:
        # Never the running task: stop() called from inside an on_reconnect callback would
        # otherwise wait on itself.
        tasks = {t for t in self._tasks if t is not asyncio.current_task()}
        for task in tasks:
            task.cancel()
        if tasks:
            # ``wait`` rather than ``gather``: it never re-raises the tasks' CancelledError, yet
            # still lets a cancellation of *this* task propagate.
            await asyncio.wait(tasks)

    async def _resubscribe(self, old_id: UUID) -> None:
        from .connection_manager import SurrealDBConnectionManager

        # The old uuid is gone with the socket; a late subscribe_live() on it must end at once.
        mark_killed(old_id)
        policy = self._policy
        delay = float(policy.reconnect_delay)
        attempt = 0
        while True:
            attempt += 1
            client: Any = None
            try:
                self._check_identity()
                client = await SurrealDBConnectionManager.get_client()
                # Again once connected: the replay itself, or a concurrent signin, may have moved it.
                self._check_identity()
                new_id = await self._start_on(client)
                break
            except asyncio.CancelledError:
                if cancelled_from_outside():
                    raise
                error: Exception = SurrealDbConnectionError("the connection dropped during the request")
            except Exception as exc:
                if not is_connection_failure(exc, client):
                    self._give_up(exc)
                    return
                error = exc
            limit = policy.reconnect_max_attempts
            if limit is not None and attempt >= limit:
                self._give_up(
                    SurrealDbConnectionError(
                        f"Lost the live query on {self._table!r} and could not resubscribe: gave up "
                        f"after {attempt} attempt{'s' if attempt > 1 else ''}. Last error: {error}"
                    )
                )
                return
            logger.warning(
                "Resubscribing the live query on %s failed (attempt %d): %s; retrying in %.2fs",
                self._table,
                attempt,
                error,
                delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, float(policy.reconnect_max_delay))

        self._client, self._live_id = client, new_id
        if self._iterator is not None:
            self._iterator.rebind(client, new_id)
        # Cleared *before* the callback: a second drop while it runs must resubscribe again.
        self._resubscribe_pending = False
        logger.info("Live query on %s resubscribed after a dropped connection (%s → %s)", self._table, old_id, new_id)
        await self._notify_reconnect(old_id, new_id)

    def _check_identity(self) -> None:
        """Refuse to reopen the live query under an identity other than the one that opened it.

        A live query is evaluated with the permissions of whoever opened it. A new connection
        comes back as the identity the manager *replays* — the stored session token, else the
        configured user — so resubscribing is only sound when that is still the identity the
        stream started under. Otherwise the stream ends with an authentication error instead
        of carrying on with different permissions, possibly the configured root user's.

        :raises SurrealDbAuthenticationError: the stream started under an identity that cannot be
            replayed (``store=False``), or the identity changed since (signin, signup,
            authenticate, invalidate, clear_session, or a session the server refused to restore).
        """
        from .connection_manager import SurrealDBConnectionManager

        started = self._identity
        if started is None:
            return
        if started[1]:
            raise SurrealDbAuthenticationError(
                f"The live query on {self._table!r} was opened under an identity the connection "
                "manager cannot restore on a new connection (signin/signup/authenticate with "
                "store=False, or clear_session() since). It is not resubscribed: a new connection "
                "would run it with different permissions."
            )
        if SurrealDBConnectionManager._identity_snapshot() != started:
            raise SurrealDbAuthenticationError(
                f"The connection's identity changed since the live query on {self._table!r} was "
                "opened (signin, signup, authenticate, invalidate, or a rejected session replay). "
                "It is not resubscribed: the new subscription would run with different permissions."
            )

    async def _start_on(self, client: Any) -> UUID:
        """Run the starter, shielded: a stop() landing mid-request must not orphan its result."""
        start = asyncio.ensure_future(self._starter(client))
        try:
            return await asyncio.shield(start)
        except asyncio.CancelledError:
            if cancelled_from_outside():
                # The LIVE SELECT may already be running on the server: kill it once it answers.
                start.add_done_callback(_kill_when_started)
            raise

    def _give_up(self, error: Exception) -> None:
        """End the stream with *error*: no subscription is left, so nothing remains to kill."""
        self._resubscribe_pending = False
        self._live_id = None
        if self._iterator is not None:
            self._iterator.fail(error)

    async def _notify_reconnect(self, old_id: UUID, new_id: UUID) -> None:
        callback = self._on_reconnect
        if callback is None:
            return
        try:
            result = callback(old_id, new_id)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("on_reconnect callback failed for the live query on %s", self._table)

    def __aiter__(self) -> LiveStream:
        return self

    async def __anext__(self) -> dict[str, Any]:
        if self._iterator is None:
            raise StopAsyncIteration
        try:
            return await self._iterator.__anext__()
        except Exception:
            # The iterator only raises to report a lost subscription (a drop, or a reconnect
            # that gave up). Nothing is left on the server, so nothing remains to kill.
            self._live_id = None
            self._iterator = None
            raise

    async def __aenter__(self) -> LiveStream:
        return await self.start()

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.stop()


def _kill_when_started(start: asyncio.Future[UUID]) -> None:
    """Kill a live query whose start outlived the stream that requested it."""
    if start.cancelled() or start.exception() is not None:
        return
    from .connection_manager import SurrealDBConnectionManager

    task = asyncio.get_running_loop().create_task(SurrealDBConnectionManager.kill(start.result()))
    _ORPHAN_KILLS.add(task)
    task.add_done_callback(_ORPHAN_KILLS.discard)


# Strong references to fire-and-forget kills, which the event loop alone would let be collected.
_ORPHAN_KILLS: set[asyncio.Task[None]] = set()


# ------------------------------------------------------------------
# Typed events (v0.20.0)
# ------------------------------------------------------------------

# The root JSON Pointer of a diff patch: SurrealDB 3.x spells it "" and 2.x spells it "/".
_ROOT_POINTERS: Final = ("", "/")


@dataclass
class ModelChangeEvent(Generic[T]):
    """One typed live-query notification — the full SurrealDB-ORM's event, field for field.

    :ivar action: ``CREATE``, ``UPDATE`` or ``DELETE``.
    :ivar instance: the model instance. For a non-diff stream it is the record after the change
        (for ``DELETE``, its last state). In diff mode ``CREATE`` carries the whole record and
        ``UPDATE`` a minimal instance holding just the ``id``; ``DELETE`` is minimal on 3.x and
        the last state on 2.x, which sends the whole record there.
    :ivar record_id: the affected record, e.g. ``"user:abc"``.
    :ivar changed_fields: diff mode only — the top-level fields the patches touch, as Python
        field names (aliases resolved).
    :ivar validation_error: set when the record does not validate against the model — e.g. a
        ``fetch()``-resolved link on a field typed ``str``, or a row another client wrote with a
        missing field. ``instance`` is then built **without validation** from the record as
        received, and the stream carries on; ``post_live_change`` is not sent for such an event.
        ``None`` for every valid event.
    :ivar raw: the notification's ``result`` as received: a dict, or in diff mode the list of
        patches — except a diff-mode ``DELETE`` on SurrealDB 2.x, which is the whole last record
        (a dict). SurrealDB's diff is JSON Patch **extended**: a changed string arrives as
        ``{"op": "change", "value": "<diff-match-patch text>"}`` rather than a ``replace``.
    """

    action: LiveAction
    instance: T
    record_id: str
    changed_fields: list[str] = field(default_factory=list)
    raw: Any = field(default_factory=dict)
    validation_error: ValidationError | None = None


def _pointer_root(path: str) -> str:
    """The first segment of a non-root JSON Pointer, unescaped (``~1`` → ``/``, ``~0`` → ``~``)."""
    return path[1:].split("/", 1)[0].replace("~1", "/").replace("~0", "~")


def minimal_instance(model: type[T], record: Any) -> T:
    """An instance holding only the id — the full ORM's rule for a change with no record."""
    # ``set_data`` is the model's own RecordID → id conversion; mypy sees pydantic's decorator
    # proxy rather than the classmethod it resolves to at runtime.
    data = cast(Any, model).set_data({"id": record})
    try:
        return model.model_validate(data)
    except ValidationError:
        return model.model_construct(**data)


def hydrate(model: type[T], record: dict[str, Any]) -> tuple[T, ValidationError | None]:
    """A model instance from a record, never raising for a record that does not validate.

    An invalid record yields an instance built **without** validation, plus the error — one bad
    row must not end a stream. Shared by live events and change-feed events.
    """
    # Copies throughout: hydration rewrites the id in place, and ``raw`` must stay as received.
    try:
        return cast(T, model.from_db(dict(record))), None
    except ValidationError as exc:
        return model.model_construct(**cast(Any, model).set_data(dict(record))), exc


def patched_fields(model: type[BaseSurrealModel], patches: Any) -> list[str]:
    """The top-level fields a JSON Patch list touches, as Python names, in order, once each."""
    changed: list[str] = []
    for patch in patches if isinstance(patches, list) else ():
        path = patch.get("path", "") if isinstance(patch, dict) else ""
        if path not in _ROOT_POINTERS and path.startswith("/"):
            name = model.to_py_field(_pointer_root(path))
            if name not in changed:
                changed.append(name)
    return changed


def to_change_event(model: type[T], envelope: dict[str, Any], *, diff: bool) -> ModelChangeEvent[T]:
    """Convert one raw notification envelope into a :class:`ModelChangeEvent`.

    In diff mode the two server lines answer differently and both are normalised here: the root
    patch path is ``""`` on 3.x and ``"/"`` on 2.x, and a ``DELETE`` is a ``replace`` of the root
    with ``None`` on 3.x but the whole last record on 2.x.

    A record that does not validate against *model* does not raise: one bad row would otherwise
    end the consumer's ``async for`` and kill the subscription. The event carries the error in
    ``validation_error`` and an unvalidated instance, the way ``QuerySet.exec()`` falls back to
    plain rows instead of failing.
    """
    record = envelope.get("record")
    result = envelope.get("result")
    payload: Any = result
    changed: list[str] = []
    if diff and isinstance(result, list):
        payload = None
        for patch in result:
            if not isinstance(patch, dict):
                continue
            path = patch.get("path", "")
            if path in _ROOT_POINTERS:
                # CREATE replaces the root with the record; DELETE (3.x) replaces it with None.
                payload = patch.get("value")
                continue
            if path.startswith("/"):
                name = model.to_py_field(_pointer_root(path))
                if name not in changed:
                    changed.append(name)
    error: ValidationError | None = None
    if isinstance(payload, dict):
        instance, error = hydrate(model, payload)
    else:
        instance = minimal_instance(model, record)
    return ModelChangeEvent(
        action=LiveAction(envelope["action"]),
        instance=instance,
        record_id=str(record) if record is not None else "",
        changed_fields=changed,
        raw=result if result is not None else {},
        validation_error=error,
    )


class LiveModelStream(Generic[T]):
    """Typed live query: async context manager and iterator of :class:`ModelChangeEvent`.

    The full SurrealDB-ORM's class, on the official SDK::

        async with User.objects().filter(role="admin").live() as stream:
            async for event in stream:
                print(event.action, event.instance, event.record_id)

    It wraps a :class:`LiveStream`, which owns the subscription — start, kill on exit, and an
    end of iteration that is identical on both server lines — and only converts each envelope.

    **``post_live_change`` handlers** run on one background task per stream, in event order, so
    a slow handler never stalls the iteration and never lets a later event's handler overtake
    it. They only receive validated instances: an event with a ``validation_error`` is yielded
    to the iterating code but not signalled. Leaving the block normally waits up to :attr:`signal_drain_timeout` seconds for the
    pending handlers to finish; leaving it on an exception cancels them.

    **Deprecated v0.19.0 form:** ``await qs.live()`` still starts a live query and returns its
    uuid, for :meth:`SurrealDBConnectionManager.subscribe_live`. That subscription belongs to the
    caller, not to this object. The object also behaves as a coroutine for that path, so
    ``asyncio.create_task(qs.live())`` and ``asyncio.run(qs.live())`` keep working. Like a
    coroutine it can be awaited once, and an awaited stream cannot also be started (nor the
    reverse): the two would be separate subscriptions, and only one would be killed.
    """

    #: Seconds a normal exit waits for pending ``post_live_change`` handlers before cancelling.
    signal_drain_timeout: float = 5.0
    #: Reconnect policy after a dropped connection — see :class:`LiveStream`.
    reconnect_delay: float = LiveStream.reconnect_delay
    reconnect_max_delay: float = LiveStream.reconnect_max_delay
    reconnect_max_attempts: int | None = LiveStream.reconnect_max_attempts

    def __init__(
        self,
        model: type[T],
        table: str,
        starter: Callable[..., Awaitable[UUID]],
        *,
        diff: bool = False,
        auto_resubscribe: bool = True,
        on_reconnect: ReconnectCallback | None = None,
    ) -> None:
        self._model = model
        self._diff = diff
        self._starter = starter
        self._stream = LiveStream(table, starter, auto_resubscribe=auto_resubscribe, on_reconnect=on_reconnect)
        self._stream._policy = self
        self._started = False
        self._uuid_start: Coroutine[Any, Any, UUID] | None = None
        self._warned_invalid = False
        self._signal_queue: asyncio.Queue[ModelChangeEvent[T] | None] | None = None
        self._signal_worker: asyncio.Task[None] | None = None

    @property
    def table(self) -> str:
        """The table this stream is subscribed to."""
        return self._stream.table

    @property
    def live_id(self) -> UUID | None:
        """The live query's uuid, or ``None`` before start and after :meth:`stop`."""
        return self._stream.live_id

    @property
    def is_active(self) -> bool:
        """Whether the subscription is currently running."""
        return self._stream.is_active

    async def start(self) -> LiveModelStream[T]:
        """Open the subscription. Calling it on a running stream is a no-op.

        :raises SurrealDbError: if this object was already awaited for a uuid.
        """
        if self._uuid_start is not None:
            raise SurrealDbError(
                "This live() stream was awaited for its uuid (the deprecated v0.19.0 form), which "
                "started a separate subscription it does not own. Call live() again for a stream."
            )
        self._started = True
        await self._stream.start()
        return self

    async def stop(self) -> None:
        """Kill the subscription and end the iteration.

        Pending ``post_live_change`` handlers get up to :attr:`signal_drain_timeout` seconds to
        finish, then are cancelled.
        """
        await self._stop(cancel_handlers=False)

    async def _stop(self, *, cancel_handlers: bool) -> None:
        try:
            await self._stream.stop()
        finally:
            await self._finish_signals(cancel=cancel_handlers)

    def __aiter__(self) -> LiveModelStream[T]:
        return self

    async def __anext__(self) -> ModelChangeEvent[T]:
        try:
            envelope = await self._stream.__anext__()
        except (StopAsyncIteration, Exception):
            # Killed elsewhere, the connection closed, or the subscription was lost: let the
            # handler task drain and exit. (A cancelled read is a BaseException and lands
            # nowhere near here — the stream stays alive for the next read.)
            self._close_signal_queue()
            raise
        event = to_change_event(self._model, envelope, diff=self._diff)
        if event.validation_error is None:
            self._emit(event)
        elif not self._warned_invalid:
            self._warned_invalid = True
            # Locations and error types only: the error's own text embeds the record's values,
            # which may be anything the table holds (hashes, emails, tokens).
            problems = [(e["loc"], e["type"]) for e in event.validation_error.errors(include_input=False)]
            logger.warning(
                "A live %s record (%s) does not validate against the model: %s. Events carry the "
                "error in `validation_error` and an unvalidated instance; post_live_change is not "
                "sent for them. Logged once per stream.",
                self._model.__name__,
                event.record_id,
                problems,
            )
        return event

    async def __aenter__(self) -> LiveModelStream[T]:
        return await self.start()

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self._stop(cancel_handlers=exc_type is not None)

    # -- deprecated v0.19.0 form: ``await qs.live()`` → uuid ---------------------------------

    def _uuid_coroutine(self) -> Coroutine[Any, Any, UUID]:
        if self._uuid_start is None:
            if self._started:
                raise SurrealDbError(
                    "This live() stream is already started; await it only for the deprecated "
                    "uuid form, on a fresh `qs.live()`. Use `stream.live_id` instead."
                )
            warnings.warn(
                "`await QuerySet.live()` returning the live query's uuid is deprecated since "
                "v0.20.0; use `async with Model.objects().live() as stream:` for typed events, "
                "or `watch()` for raw envelopes.",
                DeprecationWarning,
                stacklevel=user_stacklevel(),
            )
            self._uuid_start = cast(Coroutine[Any, Any, UUID], self._starter())
        return self._uuid_start

    def __await__(self) -> Generator[Any, None, UUID]:
        return self._uuid_coroutine().__await__()

    def send(self, value: Any) -> Any:
        """Coroutine protocol, so ``asyncio.create_task(qs.live())`` keeps working (deprecated)."""
        return self._uuid_coroutine().send(value)

    def throw(self, *args: Any) -> Any:
        """Coroutine protocol (see :meth:`send`)."""
        return self._uuid_coroutine().throw(*args)

    def close(self) -> None:
        """Coroutine protocol (see :meth:`send`). Never touches a started stream."""
        if self._uuid_start is not None:
            self._uuid_start.close()

    # -- post_live_change ------------------------------------------------------------------

    def _emit(self, event: ModelChangeEvent[T]) -> None:
        """Queue ``post_live_change`` for the handler task; never block or break the stream."""
        from .signals import post_live_change

        if not post_live_change.has_handlers(self._model):
            return
        if self._signal_queue is None:
            self._signal_queue = asyncio.Queue()
            self._signal_worker = asyncio.get_running_loop().create_task(self._run_signals(self._signal_queue))
        self._signal_queue.put_nowait(event)

    async def _run_signals(self, queue: asyncio.Queue[ModelChangeEvent[T] | None]) -> None:
        """Send queued events one at a time, in order, until the end marker."""
        from .signals import post_live_change

        while (event := await queue.get()) is not None:
            try:
                await post_live_change.send(
                    self._model,
                    instance=event.instance,
                    action=event.action,
                    record_id=event.record_id,
                    changed_fields=event.changed_fields,
                )
            except Exception:
                logger.exception("post_live_change handler failed for %s", self._model.__name__)

    def _close_signal_queue(self) -> None:
        if self._signal_queue is not None:
            self._signal_queue.put_nowait(None)

    async def _finish_signals(self, *, cancel: bool) -> None:
        worker, self._signal_worker = self._signal_worker, None
        self._close_signal_queue()
        self._signal_queue = None
        if worker is None or worker.done():
            return
        if not cancel:
            try:
                await asyncio.wait_for(asyncio.shield(worker), self.signal_drain_timeout)
                return
            except TimeoutError:
                logger.warning(
                    "post_live_change handlers for %s did not finish within %.1fs; cancelling them",
                    self._model.__name__,
                    self.signal_drain_timeout,
                )
        worker.cancel()
        # ``wait`` rather than ``await worker``: it does not re-raise the worker's own
        # CancelledError, yet still lets a cancellation of *this* task propagate.
        await asyncio.wait({worker})
