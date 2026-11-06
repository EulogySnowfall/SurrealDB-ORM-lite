"""The SDK call frontier (v0.50.0): every call the ORM makes to an SDK client goes through this module.

The ORM's *imports* of the SDK go through ``_sdk.py``; its *calls* go through here. Each function takes the SDK client
as its first argument, makes one SDK call, awaits the builder it returns once, and passes the result through
``normalize()`` — the only place SDK values become ORM values. ``tests/test_sdk_frontier.py`` fails on an SDK data call
made anywhere else in the package, and ``tests/_frontier_spy.py`` on one made at run time.

What changed with the official SDK 3.0, and how this module keeps the ORM's semantics:

- ``query()`` resolves to one result per statement, and a failing statement raises even when it is not the first.
  ``query()`` here returns the first statement's result (``None`` for an empty result list), as the ORM's internal
  single-statement calls expect; ``query_all()`` returns them all.
- The data methods return awaitable builders. Each function awaits its builder exactly once.
- ``merge()``, ``patch()`` and ``insert_relation()`` are gone from the SDK clients: they are ``update(target).merge()``,
  ``update(target).patch()`` and ``insert(table, data, relation=True)``. A missing record still answers ``None``.
- A ``str`` target is always a table name for the SDK (``"t:1"`` is the table ``t:1``): a ``str`` given here is wrapped
  in ``Table``; a record is a ``RecordID``.
- ``txn_id`` (an interactive transaction's id) is passed only when given: the SDK's HTTP methods have no such keyword,
  and no ORM path gives one to an HTTP client (transactions over HTTP are buffered).

Errors (v0.50.0): the SDK's transport and response errors reach the ORM's callers as ``SurrealDbConnectionError``,
chained to the SDK's error (``__cause__``). ``ConnectionUnavailableError`` (the connection is gone or could not be
reached) and any other ``TransportError`` are retryable; ``TransportTimeoutError`` (a deadline set with
``get_client().timeout(…)``) and ``UnexpectedResponseError`` (a response the SDK cannot read) are not. Server errors
(``ServerError`` and its subclasses) pass through unchanged.

Retired clients (v0.50.0): SDK 3.0's WebSocket client opens a new socket on any request made once its socket is gone,
and that socket is anonymous (the ORM signs in itself, it never hands the SDK credentials). ``retire()`` makes a client
the ORM no longer serves refuse every request instead (``SurrealDbConnectionError``, not retryable), then closes it;
``refuse_on_drop()`` does the refusing part as soon as a client's connection drops, before the ORM has replaced it.

``normalize()`` maps the SDK 3.0 values the ORM reads differently: SurrealDB's ``NULL`` (the SDK's ``Null``) is
``None``, a native set (``SurrealSet``) is a ``set`` — a ``list`` when a ``set`` would lose an item — and an open range
bound is ``None``. Everything else, ``PreciseDatetime`` included, is returned as the SDK gives it.

Internal: not exported, not part of the public API. ``SurrealDBConnectionManager.get_client()`` still returns the raw SDK
client; code that calls it directly gets the SDK's own semantics.
"""

import asyncio
import contextlib
import copy
import weakref
from collections.abc import Iterator
from typing import Any
from uuid import UUID

from ._sdk import (
    Null,
    RecordID,
    SdkAsyncWsConnection,
    SdkRange,
    SurrealSet,
    Table,
    TransportError,
    TransportTimeoutError,
    UnexpectedResponseError,
)
from .exceptions import SurrealDbConnectionError

__all__ = [
    "connection_error",
    "create",
    "delete",
    "insert",
    "insert_relation",
    "merge",
    "normalize",
    "patch",
    "query",
    "query_all",
    "query_raw",
    "is_retired",
    "refuse",
    "refuse_on_drop",
    "retire",
    "select",
    "update",
    "upsert",
]

#: A CRUD target: a record, a whole table, or a table name (wrapped in ``Table``).
Target = RecordID | Table | str


def _target(target: Any) -> Any:
    """*target* as the SDK must receive it: a ``str`` is a table name, wrapped in ``Table``."""
    return Table(target) if isinstance(target, str) else target


def _txn(txn_id: UUID | None) -> dict[str, Any]:
    """The ``txn_id`` keyword, only when one is given."""
    return {} if txn_id is None else {"txn_id": txn_id}


@contextlib.contextmanager
def _sdk_errors() -> Iterator[None]:
    """Re-raise the SDK's transport and response errors as ``SurrealDbConnectionError`` (see the module docstring)."""
    try:
        yield
    except (TransportError, UnexpectedResponseError) as exc:
        raise connection_error(exc) from exc


def connection_error(exc: BaseException) -> SurrealDbConnectionError:
    """The ``SurrealDbConnectionError`` an SDK transport or response error becomes, ``retryable`` set.

    Retryable: a connection that is gone or could not be reached (``ConnectionUnavailableError`` and the other
    ``TransportError``s). Not retryable: a deadline that passed (``TransportTimeoutError``) or a response the SDK cannot
    read (``UnexpectedResponseError``) — the same request would meet the same answer.
    """
    error = SurrealDbConnectionError(str(exc))
    retryable = isinstance(exc, TransportError) and not isinstance(exc, TransportTimeoutError)
    error.retryable = retryable  # type: ignore[attr-defined]
    return error


# --- queries --------------------------------------------------------------------------------------


async def query(client: Any, sql: str, variables: dict[str, Any] | None = None, *, txn_id: UUID | None = None) -> Any:
    """The first statement's result, normalised (``None`` when the server answered no statement).

    A failing statement raises, even when it is not the first (SDK 3.0).
    """
    with _sdk_errors():
        results = await client.query(sql, variables, **_txn(txn_id))
    return normalize(results[0]) if results else None


async def query_all(client: Any, sql: str, variables: dict[str, Any] | None = None, *, txn_id: UUID | None = None) -> list[Any]:
    """One normalised result per statement."""
    with _sdk_errors():
        results = await client.query(sql, variables, **_txn(txn_id))
    return [normalize(result) for result in results]


async def query_raw(
    client: Any, sql: str, variables: dict[str, Any] | None = None, *, txn_id: UUID | None = None
) -> dict[str, Any]:
    """The server's response envelope (``{"result": [{"status", "result", "time", …}]}`` or ``{"error": …}``), normalised."""
    with _sdk_errors():
        response = await client.query_raw(sql, variables, **_txn(txn_id))
    raw: dict[str, Any] = normalize(response)
    return raw


# --- CRUD -----------------------------------------------------------------------------------------


async def select(client: Any, target: Target, *, txn_id: UUID | None = None) -> Any:
    """A record (``dict``, or ``None`` when it does not exist) or a table's records (``list``)."""
    with _sdk_errors():
        result = await client.select(_target(target), **_txn(txn_id))
    return normalize(result)


async def create(client: Any, target: Target, data: Any, *, txn_id: UUID | None = None) -> Any:
    """``CREATE``: the new record, or a list of records for a table target."""
    with _sdk_errors():
        result = await client.create(_target(target), data, **_txn(txn_id))
    return normalize(result)


async def update(client: Any, target: Target, data: Any, *, txn_id: UUID | None = None) -> Any:
    """``UPDATE … CONTENT``: replaces the record(s) with *data*.

    A field missing from *data* is removed, as with SDK 2.0's ``update()`` (measured on SurrealDB 2.x, 3.x and 4.x).
    """
    with _sdk_errors():
        result = await client.update(_target(target), data, **_txn(txn_id))
    return normalize(result)


async def upsert(client: Any, target: Target, data: Any, *, txn_id: UUID | None = None) -> Any:
    """``UPSERT … CONTENT``: creates or replaces the record(s)."""
    with _sdk_errors():
        result = await client.upsert(_target(target), data, **_txn(txn_id))
    return normalize(result)


async def merge(client: Any, target: Target, data: Any, *, txn_id: UUID | None = None) -> Any:
    """``UPDATE … MERGE``: SDK 3.0's ``update(target).merge(data)``; ``None`` for a missing record."""
    with _sdk_errors():
        result = await client.update(_target(target), **_txn(txn_id)).merge(data)
    return normalize(result)


async def patch(client: Any, target: Target, ops: list[Any], *, txn_id: UUID | None = None) -> Any:
    """``UPDATE … PATCH`` (JSON Patch *ops*): SDK 3.0's ``update(target).patch(ops)``; ``None`` for a missing record."""
    with _sdk_errors():
        result = await client.update(_target(target), **_txn(txn_id)).patch(ops)
    return normalize(result)


async def insert(client: Any, table: str | Table, data: Any, *, txn_id: UUID | None = None) -> list[Any]:
    """``INSERT INTO``: the inserted records, always a list."""
    with _sdk_errors():
        result = await client.insert(_target(table), data, **_txn(txn_id))
    rows: list[Any] = normalize(result)
    return rows


async def insert_relation(client: Any, table: str | Table, data: Any, *, txn_id: UUID | None = None) -> list[Any]:
    """``INSERT RELATION INTO``: SDK 3.0's ``insert(table, data, relation=True)``; the edges, always a list."""
    with _sdk_errors():
        result = await client.insert(_target(table), data, relation=True, **_txn(txn_id))
    rows: list[Any] = normalize(result)
    return rows


async def delete(client: Any, target: Target, *, txn_id: UUID | None = None) -> Any:
    """``DELETE``: the deleted record(s)."""
    with _sdk_errors():
        result = await client.delete(_target(target), **_txn(txn_id))
    return normalize(result)


# --- retired clients ------------------------------------------------------------------------------


#: How long ``retire()`` waits for the SDK's ``close()``.
_CLOSE_TIMEOUT = 2.0


def is_retired(client: Any) -> bool:
    """Whether *client* refuses every request (:func:`refuse`)."""
    connect = getattr(client, "__dict__", {}).get("connect")
    return getattr(connect, "_orm_refusal", None) is not None


def refuse(client: Any, reason: str) -> None:
    """Make every further request on the WebSocket *client* raise ``SurrealDbConnectionError(reason)``, not retryable.

    Every request path of SDK 3.0's WebSocket client starts with ``connect()``, which opens a new, anonymous socket
    when the old one is gone. The instance's ``connect`` is replaced by one that raises instead, so the client can
    never open a socket again. Idempotent: the first reason stays. Not for HTTP clients: they hold no socket, their
    requests never call ``connect()``, and a kept one sends the token it had (measured, SDK 3.0.0), as with SDK 2.0;
    nor for the embedded engine, which has no socket either.
    """
    if is_retired(client) or SdkAsyncWsConnection is None or not isinstance(client, SdkAsyncWsConnection):
        return

    async def _refuse(*args: Any, **kwargs: Any) -> None:
        error = SurrealDbConnectionError(reason)
        error.retryable = False  # type: ignore[attr-defined]
        raise error

    # The closure holds the reason, never the client: no reference cycle.
    _refuse._orm_refusal = reason  # type: ignore[attr-defined]
    client.connect = _refuse  # type: ignore[method-assign]


def refuse_on_drop(client: Any, reason: str) -> None:
    """:func:`refuse` *client* as soon as its WebSocket's reader stops (a drop, or a close).

    A request made on the client between the drop and the ORM replacing it (a reference kept by the application, an
    open interactive transaction) then fails instead of reconnecting anonymously. A no-op for a client with no reader.
    """
    task = getattr(client, "recv_task", None)
    if not isinstance(task, asyncio.Task):
        return
    # A weak reference: the reader's callback must not keep a client the ORM let go of alive.
    held = weakref.ref(client)

    def _on_done(_task: asyncio.Task[Any]) -> None:
        dropped = held()
        if dropped is not None:
            refuse(dropped, reason)

    task.add_done_callback(_on_done)


async def retire(client: Any, reason: str) -> None:
    """Make *client* refuse every further request with ``reason`` (:func:`refuse`), then close it.

    The refusal is installed **before** ``close()``: SDK 3.0's ``close()`` never reaches ``connect()``
    (``tests/test_identity_sdk3.py::TestSdkLayout`` pins it), so the close cannot meet the refusal. The close waits at
    most two seconds, and what it raises is suppressed: the client is being given up either way.
    """
    refuse(client, reason)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(client.close(), _CLOSE_TIMEOUT)


# --- values ---------------------------------------------------------------------------------------


def normalize(value: Any) -> Any:
    """*value* as the ORM reads it: SDK 3.0's ``Null`` → ``None``, ``SurrealSet`` → ``set``, open range bound → ``None``.

    Pure: the input is never changed, and the very object comes back when nothing in it changes. Descends into
    ``dict`` and ``list`` (a changed one comes back as a plain ``dict``/``list``). A native set is a ``set`` of its
    normalised items, or a ``list`` of them when an item is unhashable or when two items are equal in Python (``1``
    and ``True``): the server keeps both, a ``set`` would drop one. A ``Range`` with a ``Null`` side comes back as a
    copy with ``None`` on that side. Every other value (``PreciseDatetime``, ``Duration``, ``RecordID``, geometries,
    ``Decimal``, ``UUID``…) is returned as it is.
    """
    if value is Null:
        return None
    if isinstance(value, SurrealSet):
        return _set_of([normalize(item) for item in value])
    if isinstance(value, dict):
        changed = False
        out: dict[Any, Any] = {}
        for key, item in value.items():
            new = normalize(item)
            changed = changed or new is not item
            out[key] = new
        return out if changed else value
    if isinstance(value, list):
        items = [normalize(item) for item in value]
        if any(new is not old for new, old in zip(items, value, strict=True)):
            return items
        return value
    if isinstance(value, SdkRange) and (value.begin is Null or value.end is Null):
        # ``Range.__init__`` turns a ``None`` side back into ``Null``: set the sides on a copy instead.
        out_range = copy.copy(value)
        if out_range.begin is Null:
            out_range.begin = None
        if out_range.end is Null:
            out_range.end = None
        return out_range
    return value


def _set_of(items: list[Any]) -> set[Any] | list[Any]:
    """*items* as a ``set``, or as a ``list`` when a ``set`` would drop one (unhashable, or equal in Python)."""
    try:
        out = set(items)
    except TypeError:
        return items
    return out if len(out) == len(items) else items
