"""Request-counting helpers on the SDK call frontier (v0.50.0), for the tests that count or hold what a client sends.

Every SDK call the ORM makes goes through ``surreal_orm_lite._client`` (one function per request, the client first).
These helpers patch those functions, not the SDK client's class: they see exactly the requests the ORM sends, whatever
the SDK does underneath (SDK 3.0's data methods return builders, and its ``query()`` no longer runs ``query_raw()``).

Each patch is keyed on ``id(client)``: the calls of other clients pass through unrecorded and unheld. The patches are
made with *monkeypatch*, so they end with the test; two helpers armed on one function (two clients, or ``record()``
and ``hold_next()``) stack.

Calls the test makes itself on the SDK client (``await client.query(...)``) are not requests the ORM sends: they are
never seen.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from typing import Any, NamedTuple, TypeVar

import pytest

from surreal_orm_lite import _client

__all__ = ["CRUD", "QUERIES", "WRITES", "Call", "hold_next", "record"]

#: The frontier's statement functions: ``(client, sql, variables=None, *, txn_id=None)``.
QUERIES = ("query", "query_all", "query_raw")
#: The frontier's write functions: ``(client, target, [data], *, txn_id=None)``.
WRITES = ("create", "insert", "insert_relation", "update", "merge", "patch", "upsert", "delete")
#: Every CRUD function of the frontier, ``select`` included.
CRUD = ("select", *WRITES)

T = TypeVar("T")


class Call(NamedTuple):
    """One frontier call: the function's *name*, the statement (``QUERIES``) or the target (``CRUD``), and the
    statement's variables (``{}`` when none were given; ``None`` for a CRUD call)."""

    name: str
    target: Any
    variables: dict[str, Any] | None


def _call(name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Call:
    """The ``Call`` of ``_client.<name>(client, *args, **kwargs)`` (the client already taken off *args*)."""
    first = "sql" if name in QUERIES else ("table" if name in ("insert", "insert_relation") else "target")
    target = args[0] if args else kwargs.get(first)
    if name not in QUERIES:
        return Call(name, target, None)
    variables = args[1] if len(args) > 1 else kwargs.get("variables")
    return Call(name, target, dict(variables or {}))


def _function(name: str) -> Callable[..., Any]:
    if name not in QUERIES and name not in CRUD:
        raise ValueError(f"{name!r} is not a request function of surreal_orm_lite._client")
    function: Callable[..., Any] = getattr(_client, name)
    return function


def record(
    monkeypatch: pytest.MonkeyPatch,
    client: Any,
    *names: str,
    entry: Callable[[Call], T] | None = None,
) -> list[Any]:
    """Record each call of the frontier functions *names* made for *client* from now on, then run it as usual.

    Returns the live list of ``Call``s, in the order the calls started; *entry* turns each ``Call`` into what is kept
    instead (``lambda call: call.target`` keeps the statements). A call that raises is recorded too: it was sent.
    """
    if not names:
        raise ValueError("record() needs at least one function name")
    calls: list[Any] = []
    owner = id(client)

    def wrap(name: str, real: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(real)
        async def recording(target_client: Any, *args: Any, **kwargs: Any) -> Any:
            if id(target_client) == owner:
                call = _call(name, args, kwargs)
                calls.append(call if entry is None else entry(call))
            return await real(target_client, *args, **kwargs)

        return recording

    for name in names:
        monkeypatch.setattr(_client, name, wrap(name, _function(name)))
    return calls


def hold_next(monkeypatch: pytest.MonkeyPatch, client: Any, *names: str) -> tuple[asyncio.Event, asyncio.Event]:
    """Hold the next call of one of the frontier functions *names* made for *client*, **after** its reply.

    Returns ``(reached, release)``. The call runs for real first, so its result is computed before whatever the test
    does while it is held; then ``reached`` is set and the call waits for ``release`` before returning. One-shot: the
    hold is disarmed before the call runs, so the calls made meanwhile (the test's own, through the ORM) pass through.
    """
    if not names:
        raise ValueError("hold_next() needs at least one function name")
    reached, release = asyncio.Event(), asyncio.Event()
    owner = id(client)
    armed = [True]

    def wrap(real: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(real)
        async def holding(target_client: Any, *args: Any, **kwargs: Any) -> Any:
            hold = id(target_client) == owner and armed[0]
            if hold:
                armed[0] = False
            result = await real(target_client, *args, **kwargs)
            if hold:
                reached.set()
                await release.wait()
            return result

        return holding

    for name in names:
        monkeypatch.setattr(_client, name, wrap(_function(name)))
    return reached, release
