"""Change feeds (v0.21.0): a table's durable log of writes, read with a resumable cursor.

A table defined with ``DEFINE TABLE … CHANGEFEED <retention>`` keeps every write for that long,
and ``SHOW CHANGES FOR TABLE … SINCE <position> LIMIT <n>`` reads it back. Unlike a live query,
nothing is lost while the reader is away: :class:`ChangeModelStream` polls the log and hands out
:class:`~surreal_orm_lite.live.ModelChangeEvent` objects — the full SurrealDB-ORM's API — and its
:attr:`~ChangeModelStream.cursor` resumes exactly where it stopped, even in another process.

**The cursor is not what the server returns, and the two lines disagree on why.** Measured on
2.7.0, 3.3.2 and 3.1.5, every entry carries a ``versionstamp``, but:

* on 3.x ``SINCE`` takes that stamp and is inclusive, so the next position is ``stamp + 1``;
* on 2.x the stamp is a counter shifted left 16 bits and ``SINCE`` takes the *counter*, so the
  next position is ``(stamp >> 16) + 1``. Resuming a 2.x feed with the stamp it returned yields
  nothing at all, silently — the full ORM's change-feed stream does exactly that.

The ORM reads the server line once per stream (``version()``) and always stores the next
``SINCE`` value, so ``changes(since=stream.cursor)`` is correct on both. ``SINCE <datetime>`` is
worse still: always empty on 3.x, and on 2.x it also returns changes from before the moment. So a
datetime is converted to the 3.x stamp layout (``unix_ms << 16``, exact), passed through on 2.x
(documented as at-least-once), and "from now" on 2.x is found by searching the counter instead.

Polling is plain ``query()``, so it works over HTTP as well as WebSocket. A dropped connection is
retried with backoff like a live stream; nothing is lost, because the next poll starts from the
cursor.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from .enum import LiveAction
from .exceptions import SurrealDbConnectionError, SurrealDbError
from .live import (
    ModelChangeEvent,
    cancelled_from_outside,
    hydrate,
    is_connection_failure,
    minimal_instance,
    patched_fields,
)

if TYPE_CHECKING:
    from .model_base import BaseSurrealModel

__all__ = ["ChangeModelStream"]

logger = logging.getLogger(__name__)

T = TypeVar("T", bound="BaseSurrealModel")

#: What ``changes(since=…)`` accepts: a cursor, a moment, an ISO-8601 string, or ``None`` (now).
Since = int | datetime | str | None

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_VERSION = re.compile(r"(\d+)\.\d+")
# SurrealQL integers are signed 64-bit: the 2.x counter search must stay within them.
_MAX_COUNTER = (1 << 63) - 1


def next_cursor(versionstamp: int, *, major: int) -> int:
    """The ``SINCE`` value that resumes right after the entry stamped *versionstamp*."""
    return (versionstamp >> 16) + 1 if major < 3 else versionstamp + 1


def versionstamp_at(moment: datetime) -> int:
    """SurrealDB 3.x's versionstamp for *moment*: unix milliseconds shifted left 16 bits.

    The server's own hybrid-clock layout (measured on 3.1.5 and 3.3.2), not a documented
    contract; ``tests/test_change_feeds.py`` pins it, so a server that changes it fails CI.
    """
    return ((moment - _EPOCH) // timedelta(milliseconds=1)) << 16


def since_sql(since: int | datetime, *, major: int) -> str:
    """The SurrealQL after ``SINCE`` for a validated position. Never user text: ints and dates only."""
    if isinstance(since, int):
        return str(since)
    if major >= 3:
        return str(versionstamp_at(since))
    return f"d'{since.astimezone(UTC).isoformat().replace('+00:00', 'Z')}'"


def _validate_since(since: Any) -> int | datetime | None:
    if since is None:
        return None
    if isinstance(since, bool) or not isinstance(since, (int, datetime, str)):
        raise TypeError(f"since= takes a cursor (int), a datetime or an ISO-8601 string, not {type(since).__name__}")
    if isinstance(since, int):
        if since < 0:
            raise ValueError(f"since= must be a cursor >= 0, got {since}")
        return since
    moment: datetime
    if isinstance(since, str):
        try:
            moment = datetime.fromisoformat(since)
        except ValueError:
            raise ValueError(f"since= {since!r} is not an ISO-8601 date-time") from None
    else:
        moment = since
    # A naive datetime means UTC — the rule the ORM applies to every datetime it sends.
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _record_id(record: Any) -> str:
    rid = record.get("id") if isinstance(record, dict) else None
    return str(rid) if rid is not None else ""


def feed_events(model: type[T], entry: dict[str, Any]) -> list[ModelChangeEvent[T]]:
    """The model events of one change-feed entry (one transaction), in order.

    Shapes, measured identical on both lines unless noted: ``{"update": <record>}`` for a create
    *or* an update (a change feed does not record which), ``{"delete": {"id": …}}``, and with
    ``INCLUDE ORIGINAL`` ``{"current": <record>, "update": [<reverse patches>]}`` for an update
    and, on 3.3.x only, ``{"delete": {"id": …, "original": <record>}}``. Anything else — the
    ``define_table`` entry a definition leaves — is not a record change and is skipped.
    """
    events: list[ModelChangeEvent[T]] = []
    for item in entry.get("changes") or ():
        if not isinstance(item, dict):
            continue
        current, update, delete = item.get("current"), item.get("update"), item.get("delete")
        if isinstance(current, dict):
            instance, error = hydrate(model, current)
            events.append(
                ModelChangeEvent(
                    action=LiveAction.UPDATE,
                    instance=instance,
                    record_id=_record_id(current),
                    changed_fields=patched_fields(model, update),
                    raw=item,
                    validation_error=error,
                )
            )
        elif isinstance(update, dict):
            instance, error = hydrate(model, update)
            events.append(
                ModelChangeEvent(
                    action=LiveAction.UPDATE,
                    instance=instance,
                    record_id=_record_id(update),
                    raw=update,
                    validation_error=error,
                )
            )
        elif isinstance(delete, dict):
            original = delete.get("original")
            error = None
            if isinstance(original, dict):
                instance, error = hydrate(model, original)
            else:
                instance = minimal_instance(model, delete.get("id"))
            events.append(
                ModelChangeEvent(
                    action=LiveAction.DELETE,
                    instance=instance,
                    record_id=_record_id(delete),
                    raw=delete,
                    validation_error=error,
                )
            )
    return events


class ChangeModelStream(Generic[T]):
    """Async iterator over a table's change feed — the full SurrealDB-ORM's class::

        stream = Order.objects().changes(since=saved_cursor)   # None = from now
        async for event in stream:
            await publish(event.action, event.instance)
            save(stream.cursor)                                 # after processing

    **Cursor contract — at-least-once, per transaction.** :attr:`cursor` moves past an entry (one
    transaction) when that entry's *last* event is yielded. Saving it after processing each event
    and resuming with ``changes(since=cursor)`` never loses a change; if the process died halfway
    through a multi-write transaction, that transaction's events are delivered again.

    A change feed records writes, not their kind: a creation arrives as ``UPDATE``. With
    ``INCLUDE ORIGINAL`` on the table, updates also carry ``changed_fields``.
    """

    #: Seconds before the first retry after a connection failure. Doubles after each failure.
    reconnect_delay: float = 0.5
    #: Upper bound of the delay between two retries, in seconds.
    reconnect_max_delay: float = 30.0
    #: Attempts before giving up and raising from the iteration; ``None`` retries forever.
    reconnect_max_attempts: int | None = 10

    def __init__(
        self,
        model: type[T],
        table: str,
        *,
        since: Since = None,
        poll_interval: float = 0.1,
        batch_size: int = 100,
    ) -> None:
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise ValueError(f"batch_size must be an int >= 1, got {batch_size!r}")
        if poll_interval < 0:
            raise ValueError(f"poll_interval must be >= 0, got {poll_interval!r}")
        self._model = model
        self._table = table
        self._since = _validate_since(since)
        self._poll_interval = poll_interval
        self._batch_size = batch_size
        self._major: int | None = None
        self._cursor: int | None = None
        # The first poll's position when it is not a cursor yet: a datetime on 2.x.
        self._since_clause: str | None = None
        # Events not yet yielded, each with the cursor to adopt once it is (set on an entry's last).
        self._pending: deque[list[Any]] = deque()
        self._stopped = False

    @property
    def table(self) -> str:
        """The table whose change feed is read."""
        return self._table

    @property
    def cursor(self) -> int | None:
        """Where to resume: pass it back as ``changes(since=…)``. Opaque, and tied to the server line.

        ``None`` before the stream has started — and on SurrealDB 2.x, after starting from a
        datetime, until the first change is read (a 2.x datetime has no exact position).
        """
        return self._cursor

    def stop(self) -> None:
        """End the iteration within one ``poll_interval``. Events not yet yielded are dropped,
        and :attr:`cursor` still points at the first of them."""
        self._stopped = True

    async def start(self) -> ChangeModelStream[T]:
        """Fix the starting position now rather than at the first read. Idempotent.

        With ``since=None`` "now" is the moment of this call, so a write made between ``start()``
        and the first read is delivered.

        :raises SurrealDbError: if the table does not exist or has no change feed.
        """
        if self._major is not None:
            return self
        version = await self._with_retry(lambda client: client.version())
        match = _VERSION.search(str(version))
        major = int(match.group(1)) if match else 3
        await self._check_changefeed()
        since = self._since
        if isinstance(since, int):
            self._cursor = since
        elif isinstance(since, datetime):
            if major >= 3:
                self._cursor = versionstamp_at(since)
            else:
                self._since_clause = since_sql(since, major=major)
        elif major >= 3:
            self._cursor = versionstamp_at(await self._server_now())
        else:
            self._cursor = await self._end_of_feed_2x()
        self._major = major
        return self

    def __aiter__(self) -> ChangeModelStream[T]:
        return self

    async def __anext__(self) -> ModelChangeEvent[T]:
        while True:
            if self._stopped:
                raise StopAsyncIteration
            if self._pending:
                event, after = self._pending.popleft()
                if after is not None:
                    self._cursor = after
                return event  # type: ignore[no-any-return]
            await self.start()
            full = await self._poll()
            if not self._pending and not full and not self._stopped:
                await asyncio.sleep(self._poll_interval)

    # -- internals ---------------------------------------------------------------------------

    async def _poll(self) -> bool:
        """Read one page into the pending events. Whether the page was full (read on at once)."""
        assert self._major is not None
        position = str(self._cursor) if self._cursor is not None else self._since_clause
        entries = await self._show(position, self._batch_size)
        for entry in entries:
            self._accept(entry)
        return len(entries) >= self._batch_size

    def _accept(self, entry: dict[str, Any]) -> None:
        assert self._major is not None
        stamp = entry.get("versionstamp")
        after = next_cursor(stamp, major=self._major) if isinstance(stamp, int) else None
        events = feed_events(self._model, entry)
        if events:
            self._pending.extend([event, None] for event in events[:-1])
            self._pending.append([events[-1], after])
        elif after is not None:
            # No record change (a definition): skipped, but the cursor must still move past it —
            # with the last pending event if there is one, so a resume never skips that event.
            if self._pending:
                self._pending[-1][1] = after
            else:
                self._cursor = after

    async def _show(self, position: str | None, limit: int) -> list[dict[str, Any]]:
        sql = f"SHOW CHANGES FOR TABLE {self._table} SINCE {position} LIMIT {limit};"
        result = await self._with_retry(lambda client: client.query(sql, {}))
        return [entry for entry in result if isinstance(entry, dict)] if isinstance(result, list) else []

    async def _server_now(self) -> datetime:
        """The server's clock, not ours: versionstamps are stamped by the server."""
        now = await self._with_retry(lambda client: client.query("RETURN time::now();", {}))
        if not isinstance(now, datetime):
            now = datetime.fromisoformat(str(now))
        return now if now.tzinfo is not None else now.replace(tzinfo=UTC)

    async def _end_of_feed_2x(self) -> int:
        """The 2.x cursor right after the newest entry, found without reading the feed.

        ``SINCE <counter> LIMIT 1`` is empty exactly when no entry is at or after the counter, so
        the end is the smallest counter with an empty answer: doubling, then bisecting — about
        2·log2(counter) tiny queries. (2.x's ``SINCE <datetime>`` cannot answer this: it also
        returns changes from before the moment.)
        """

        async def empty(counter: int) -> bool:
            return not await self._show(str(counter), 1)

        if await empty(0):
            return 0
        low, high = 0, 1
        while not await empty(high):
            low = high
            if high >= _MAX_COUNTER:  # pragma: no cover - a 2.x counter never gets near 2**63
                return high
            high = min(high * 2, _MAX_COUNTER)
        while high - low > 1:
            middle = (low + high) // 2
            if await empty(middle):
                high = middle
            else:
                low = middle
        return high

    async def _check_changefeed(self) -> None:
        """Refuse a table with no change feed: ``SHOW CHANGES`` would just answer ``[]`` forever."""
        try:
            info = await self._with_retry(lambda client: client.query("INFO FOR DB;", {}))
        except SurrealDbConnectionError:
            raise
        except Exception as exc:
            # A record user may not be allowed INFO FOR DB; the check is a courtesy, not a gate.
            logger.debug("Could not read INFO FOR DB to check %s for a change feed: %s", self._table, exc)
            return
        tables = info.get("tables") if isinstance(info, dict) else None
        if not isinstance(tables, dict):
            return
        definition = tables.get(self._table)
        fix = f"DEFINE TABLE {self._table} SCHEMALESS CHANGEFEED 7d"
        if definition is None:
            raise SurrealDbError(
                f"Cannot read the change feed of {self._table!r}: the table does not exist. "
                f"Define it with a change feed first, e.g. {fix}."
            )
        if "CHANGEFEED" not in str(definition).upper():
            raise SurrealDbError(
                f"Table {self._table!r} has no change feed, so SHOW CHANGES would stay empty "
                f"forever. Add one, e.g. DEFINE TABLE OVERWRITE … CHANGEFEED 7d (as in: {fix}); "
                f"only writes made after that are recorded."
            )

    async def _with_retry(self, operation: Callable[[Any], Awaitable[Any]]) -> Any:
        """Run *operation* on the manager's client, retrying connection failures with backoff."""
        from .connection_manager import SurrealDBConnectionManager

        delay = float(self.reconnect_delay)
        attempt = 0
        while True:
            attempt += 1
            client: Any = None
            try:
                client = await SurrealDBConnectionManager.get_client()
                return await operation(client)
            except asyncio.CancelledError:
                if cancelled_from_outside():
                    raise
                error: Exception = SurrealDbConnectionError("the connection dropped during the request")
            except Exception as exc:
                if not is_connection_failure(exc, client):
                    raise
                error = exc
            limit = self.reconnect_max_attempts
            if limit is not None and attempt >= limit:
                raise SurrealDbConnectionError(
                    f"Lost the connection while reading the change feed of {self._table!r}: gave up "
                    f"after {attempt} attempt{'s' if attempt > 1 else ''}. Last error: {error}. "
                    f"Resume later with changes(since=stream.cursor)."
                )
            logger.warning(
                "Reading the change feed of %s failed (attempt %d): %s; retrying in %.2fs",
                self._table,
                attempt,
                error,
                delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, float(self.reconnect_max_delay))
