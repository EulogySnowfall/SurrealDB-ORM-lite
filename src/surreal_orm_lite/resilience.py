"""Connection resilience (v0.51.0): how a WebSocket connection reconnects after its socket drops.

:class:`ReconnectPolicy` is the official SDK's ``ReconnectOptions`` under the ORM's names. It is given per connection,
to :meth:`SurrealDBConnectionManager.set_connection` or :meth:`~SurrealDBConnectionManager.add_connection`
(``reconnect=``), and read back from :meth:`~SurrealDBConnectionManager.get_config`.

Internal helpers of the connection manager live here too: the **identity key** of a token (:func:`identity_key`,
:func:`key_hash`), the **identity stamp** a client carries (:class:`IdentityStamp`) and the call of a connection's
event listeners (:func:`call_listeners`).
"""

import asyncio
import base64
import binascii
import dataclasses
import hashlib
import inspect
import itertools
import json
import logging
from collections.abc import Callable, Iterable
from typing import Any, Literal, NamedTuple

from ._sdk import ReconnectOptions

__all__ = ["ReconnectPolicy"]

#: What the connection manager put on a client when it stamped it: the configured identity (``set_connection()``'s
#: user), the stored session, or an identity set with ``store=False``.
StampKind = Literal["configured", "stored", "unstored"]

#: The claims a token carries about itself rather than about its identity: they change at every issue.
_TOKEN_CLAIMS = frozenset({"exp", "iat", "nbf", "jti"})

#: The serial of the next identity stamp: one counter for the process, so two stamps never share one.
_STAMP_SERIALS = itertools.count(1)

#: The tasks of coroutine listeners, kept referenced until they finish.
_LISTENER_TASKS: set[asyncio.Task[Any]] = set()


@dataclasses.dataclass(frozen=True)
class ReconnectPolicy:
    """How a WebSocket connection reconnects after its socket drops: the official SDK's ``ReconnectOptions``, with
    the ORM's names.

    ::

        SurrealDBConnectionManager.set_connection(
            "ws://db:8000/rpc", "root", "root", "shop", "main", reconnect=ReconnectPolicy(attempts=None, max_delay=10)
        )

    The defaults are the SDK's. The values are checked by the SDK when the policy is built: a wrong type raises
    ``TypeError`` and a value out of range ``ValueError``, with the SDK's message (which uses the SDK's names:
    ``retry_delay`` for ``delay``, ``retry_delay_max`` for ``max_delay``, ``retry_delay_multiplier`` for ``multiplier``,
    ``retry_delay_jitter`` for ``jitter``).

    :param attempts: how many reconnect attempts are made before giving up; ``None`` for no limit; ``0`` gives up at
        once.
    :param delay: the base wait between attempts, in seconds.
    :param max_delay: the longest wait between attempts, in seconds.
    :param multiplier: what each wait is multiplied by for the next.
    :param jitter: the fraction (0 to 1) of each wait it is offset by at random, so that many clients do not all come
        back at once.
    """

    attempts: int | None = 5
    delay: float = 1.0
    max_delay: float = 60.0
    multiplier: float = 2.0
    jitter: float = 0.1

    def __post_init__(self) -> None:
        # The SDK validates its options when they are built: build them once, so a bad value fails here.
        self.to_sdk()

    def to_sdk(self) -> ReconnectOptions:
        """The SDK's ``ReconnectOptions`` for this policy (``attempts=None`` is the SDK's ``-1``, no limit)."""
        return ReconnectOptions(
            enabled=True,
            attempts=-1 if self.attempts is None else self.attempts,
            retry_delay=self.delay,
            retry_delay_max=self.max_delay,
            retry_delay_multiplier=self.multiplier,
            retry_delay_jitter=self.jitter,
        )


def identity_key(token: str) -> dict[str, Any] | str:
    """The identity *token* stands for: its claims without ``exp``, ``iat``, ``nbf`` and ``jti``.

    Two tokens the server issued to the same user, access method, namespace and database key the same, so a renewed
    token is not a new identity; another user, access method, namespace or database keys differently. The claims are
    read **without verifying the signature**: the key only compares tokens the server issued to the ORM, it never
    authorises anything. A token whose claims cannot be read keys as itself.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return token
    payload = parts[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (binascii.Error, ValueError):
        return token
    if not isinstance(claims, dict):
        return token
    return {name: value for name, value in claims.items() if name not in _TOKEN_CLAIMS}


def key_hash(key: dict[str, Any] | str) -> str:
    """The SHA-256 of an identity key's canonical JSON (sorted keys): what the manager keeps instead of a token."""
    canonical = json.dumps(key, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


class IdentityStamp(NamedTuple):
    """The identity the connection manager set on a client, recorded when it set it.

    Still read as ``(fingerprint, replayable)`` by index. ``same_identity()`` compares those two only: what a
    "same identity" check means. Comparing whole stamps means "this very stamp": ``serial`` is new at every stamp.

    :param fingerprint: what a connection built from the settings and the stored session at that moment comes back
        as, hashed (``SurrealDBConnectionManager._fingerprint``).
    :param replayable: whether a new connection restores that identity (``False`` after ``store=False``).
    :param kind: what the manager put on the client (:data:`StampKind`).
    :param serial: new at every stamp, for the whole process.
    """

    fingerprint: str
    replayable: bool
    kind: StampKind
    serial: int

    @classmethod
    def new(cls, fingerprint: str, replayable: bool, kind: StampKind) -> "IdentityStamp":
        """A stamp with the next serial."""
        return cls(fingerprint, replayable, kind, next(_STAMP_SERIALS))

    def same_identity(self, other: "IdentityStamp | None") -> bool:
        """Whether *other* records the same identity: same fingerprint, same ``replayable`` (``kind`` and ``serial``
        aside)."""
        return other is not None and (self.fingerprint, self.replayable) == (other.fingerprint, other.replayable)


def call_listeners(callbacks: Iterable[Callable[..., Any]], args: tuple[Any, ...], logger: logging.Logger) -> None:
    """Call each of *callbacks* with *args*, in the running task or thread.

    A coroutine function is scheduled as a task on the running event loop (dropped with a warning when there is
    none). An exception is logged on *logger* and ignored: a listener never breaks the ORM's call that announced the
    change, nor the listeners after it.
    """
    for callback in callbacks:
        try:
            if inspect.iscoroutinefunction(callback):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    logger.warning("Connection event listener %r not run: no running event loop.", callback)
                    continue
                task = loop.create_task(callback(*args))
                _LISTENER_TASKS.add(task)
                task.add_done_callback(_listener_done(logger, callback))
            else:
                callback(*args)
        except Exception:
            logger.exception("Connection event listener %r raised; ignored.", callback)


def _listener_done(logger: logging.Logger, callback: Callable[..., Any]) -> Callable[[asyncio.Task[Any]], None]:
    """The done-callback of a coroutine listener's task: forget the task, log what it raised."""

    def done(task: asyncio.Task[Any]) -> None:
        _LISTENER_TASKS.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("Connection event listener %r raised; ignored.", callback, exc_info=task.exception())

    return done
