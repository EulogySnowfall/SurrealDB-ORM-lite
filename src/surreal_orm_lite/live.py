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

**What still hangs.** A WebSocket that drops on its own — no ``kill()``, no
``close_connection()`` — leaves readers suspended, because nothing in the SDK reports the
close to a live-query subscriber. Calling ``kill()`` or closing the connection through the
ORM always releases them: ``close_connection()`` on its own loop, ``close_all_connections()`` on
every loop. Automatic resubscribe is v0.21.0.
"""

from __future__ import annotations

import asyncio
import logging
import warnings
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine, Generator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Generic, TypeVar, cast
from uuid import UUID

from pydantic import ValidationError

from .enum import LiveAction
from .exceptions import SurrealDbError, SurrealDbNotFoundError
from .utils import user_stacklevel

if TYPE_CHECKING:
    from .model_base import BaseSurrealModel

__all__ = ["LiveIterator", "LiveModelStream", "LiveStream", "ModelChangeEvent"]

logger = logging.getLogger(__name__)

T = TypeVar("T", bound="BaseSurrealModel")

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

    The iteration ends when the live query is killed or its connection is closed. Call
    :meth:`aclose` to stop reading early without killing the live query.
    """

    def __init__(self, client: Any, query_uuid: str | UUID, queue: asyncio.Queue[Any] | None) -> None:
        self._client = client
        self._query_uuid = query_uuid
        self._queue = queue

    def __aiter__(self) -> LiveIterator:
        return self

    async def __anext__(self) -> dict[str, Any]:
        if self._queue is None:
            raise StopAsyncIteration
        envelope = await self._queue.get()
        if envelope is _STREAM_END or (isinstance(envelope, dict) and envelope.get("action") == LiveAction.KILLED):
            self._detach()
            raise StopAsyncIteration
        return envelope  # type: ignore[no-any-return]

    async def aclose(self) -> None:
        """Stop reading and drop this reader's buffer. The live query itself keeps running."""
        self._detach()

    def _detach(self) -> None:
        if self._queue is None:
            return
        queue, self._queue = self._queue, None
        unregister_subscriber(self._client, self._query_uuid, queue)


def open_stream(client: Any, query_uuid: str | UUID) -> LiveIterator:
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
    return LiveIterator(client, query_uuid, register_subscriber(client, query_uuid))


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
    """

    def __init__(self, table: str, starter: Any) -> None:
        self._table = table
        self._starter = starter
        self._live_id: UUID | None = None
        self._client: Any = None
        self._iterator: LiveIterator | None = None

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
        self._client = await SurrealDBConnectionManager.get_client()
        self._live_id = await self._starter()
        self._iterator = open_stream(self._client, self._live_id)
        return self

    async def stop(self) -> None:
        """Kill the subscription and end the iteration. Safe before start and to repeat."""
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


def _minimal_instance(model: type[T], record: Any) -> T:
    """An instance holding only the id — the full ORM's rule for a change with no record."""
    # ``set_data`` is the model's own RecordID → id conversion; mypy sees pydantic's decorator
    # proxy rather than the classmethod it resolves to at runtime.
    data = cast(Any, model).set_data({"id": record})
    try:
        return model.model_validate(data)
    except ValidationError:
        return model.model_construct(**data)


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
        # Copies throughout: hydration rewrites the id in place, and ``raw`` must stay as received.
        try:
            instance = cast(T, model.from_db(dict(payload)))
        except ValidationError as exc:
            error = exc
            instance = model.model_construct(**cast(Any, model).set_data(dict(payload)))
    else:
        instance = _minimal_instance(model, record)
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

    def __init__(
        self,
        model: type[T],
        table: str,
        starter: Callable[[], Awaitable[UUID]],
        *,
        diff: bool = False,
    ) -> None:
        self._model = model
        self._diff = diff
        self._starter = starter
        self._stream = LiveStream(table, starter)
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
        except StopAsyncIteration:
            # Killed elsewhere or the connection closed: let the handler task drain and exit.
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
