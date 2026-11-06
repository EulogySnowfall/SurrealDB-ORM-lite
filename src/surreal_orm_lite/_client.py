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

``normalize()`` maps the SDK 3.0 values the ORM reads differently: SurrealDB's ``NULL`` (the SDK's ``Null``) is
``None``, a native set (``SurrealSet``) is a ``set`` — a ``list`` when a ``set`` would lose an item — and an open range
bound is ``None``. Everything else, ``PreciseDatetime`` included, is returned as the SDK gives it.

Internal: not exported, not part of the public API. ``SurrealDBConnectionManager.get_client()`` still returns the raw SDK
client; code that calls it directly gets the SDK's own semantics.
"""

import copy
from typing import Any
from uuid import UUID

from ._sdk import Null, RecordID, SdkRange, SurrealSet, Table

__all__ = [
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


# --- queries --------------------------------------------------------------------------------------


async def query(client: Any, sql: str, variables: dict[str, Any] | None = None, *, txn_id: UUID | None = None) -> Any:
    """The first statement's result, normalised (``None`` when the server answered no statement).

    A failing statement raises, even when it is not the first (SDK 3.0).
    """
    results = await client.query(sql, variables, **_txn(txn_id))
    return normalize(results[0]) if results else None


async def query_all(client: Any, sql: str, variables: dict[str, Any] | None = None, *, txn_id: UUID | None = None) -> list[Any]:
    """One normalised result per statement."""
    results = await client.query(sql, variables, **_txn(txn_id))
    return [normalize(result) for result in results]


async def query_raw(
    client: Any, sql: str, variables: dict[str, Any] | None = None, *, txn_id: UUID | None = None
) -> dict[str, Any]:
    """The server's response envelope (``{"result": [{"status", "result", "time", …}]}`` or ``{"error": …}``), normalised."""
    raw: dict[str, Any] = normalize(await client.query_raw(sql, variables, **_txn(txn_id)))
    return raw


# --- CRUD -----------------------------------------------------------------------------------------


async def select(client: Any, target: Target, *, txn_id: UUID | None = None) -> Any:
    """A record (``dict``, or ``None`` when it does not exist) or a table's records (``list``)."""
    return normalize(await client.select(_target(target), **_txn(txn_id)))


async def create(client: Any, target: Target, data: Any, *, txn_id: UUID | None = None) -> Any:
    """``CREATE``: the new record, or a list of records for a table target."""
    return normalize(await client.create(_target(target), data, **_txn(txn_id)))


async def update(client: Any, target: Target, data: Any, *, txn_id: UUID | None = None) -> Any:
    """``UPDATE … CONTENT``: replaces the record(s) with *data*.

    A field missing from *data* is removed, as with SDK 2.0's ``update()`` (measured on SurrealDB 2.x, 3.x and 4.x).
    """
    return normalize(await client.update(_target(target), data, **_txn(txn_id)))


async def upsert(client: Any, target: Target, data: Any, *, txn_id: UUID | None = None) -> Any:
    """``UPSERT … CONTENT``: creates or replaces the record(s)."""
    return normalize(await client.upsert(_target(target), data, **_txn(txn_id)))


async def merge(client: Any, target: Target, data: Any, *, txn_id: UUID | None = None) -> Any:
    """``UPDATE … MERGE``: SDK 3.0's ``update(target).merge(data)``; ``None`` for a missing record."""
    return normalize(await client.update(_target(target), **_txn(txn_id)).merge(data))


async def patch(client: Any, target: Target, ops: list[Any], *, txn_id: UUID | None = None) -> Any:
    """``UPDATE … PATCH`` (JSON Patch *ops*): SDK 3.0's ``update(target).patch(ops)``; ``None`` for a missing record."""
    return normalize(await client.update(_target(target), **_txn(txn_id)).patch(ops))


async def insert(client: Any, table: str | Table, data: Any, *, txn_id: UUID | None = None) -> list[Any]:
    """``INSERT INTO``: the inserted records, always a list."""
    rows: list[Any] = normalize(await client.insert(_target(table), data, **_txn(txn_id)))
    return rows


async def insert_relation(client: Any, table: str | Table, data: Any, *, txn_id: UUID | None = None) -> list[Any]:
    """``INSERT RELATION INTO``: SDK 3.0's ``insert(table, data, relation=True)``; the edges, always a list."""
    rows: list[Any] = normalize(await client.insert(_target(table), data, relation=True, **_txn(txn_id)))
    return rows


async def delete(client: Any, target: Target, *, txn_id: UUID | None = None) -> Any:
    """``DELETE``: the deleted record(s)."""
    return normalize(await client.delete(_target(target), **_txn(txn_id)))


# --- retired clients ------------------------------------------------------------------------------


async def retire(client: Any, reason: str) -> None:
    """Make *client* refuse every further request with ``reason``, then close it.

    Not implemented yet: the ORM's clients are retired once their construction moves to SDK 3.0.
    """
    raise NotImplementedError("retire() is not implemented yet")


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
