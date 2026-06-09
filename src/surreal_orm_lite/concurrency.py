# src/surreal_orm_lite/concurrency.py
"""Optimistic-concurrency helpers: conflict detection + retry decorator.

SurrealDB rolls a transaction back with a *retryable* error when a concurrent
writer modified the same data. It marks such errors with the phrase
"This transaction can be retried". ``retry_on_conflict`` re-runs the wrapped
coroutine when (and only when) ``is_conflict_error`` recognises that marker.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from .exceptions import SurrealDbConflictError

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])

# Substrings (matched case-insensitively) that mark a RETRYABLE transaction
# conflict. Anchored on SurrealDB's own "can be retried" marker, present on both
# 2.6.x and 3.x. Deliberately NARROWER than the full ORM (which also matches
# "failed transaction"/"conflict"): the non-retryable filler statement
# "The query was not executed due to a failed transaction" must NOT match, or a
# duplicate-key failure inside a transaction would be retried pointlessly.
_CONFLICT_SIGNALS = (
    "can be retried",
    "transaction conflict",
    "read or write conflict",
    "write conflict",
)


def is_conflict_error(error: BaseException) -> bool:
    """Return True if ``error`` is a retryable transaction conflict.

    A :class:`SurrealDbConflictError` always qualifies. Otherwise the exception
    and its ``__cause__`` / ``__context__`` chain are scanned for one of the
    conflict signal phrases (so a conflict wrapped in another exception is still
    recognised).
    """
    if isinstance(error, SurrealDbConflictError):
        return True
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = str(current).lower()
        if any(signal in text for signal in _CONFLICT_SIGNALS):
            return True
        current = current.__cause__ or current.__context__
    return False


def retry_on_conflict(
    max_retries: int = 3,
    base_delay: float = 0.05,
    max_delay: float = 2.0,
    backoff_factor: float = 2.0,
    jitter: bool = True,
) -> Callable[[F], F]:
    """Retry an async function on a retryable transaction conflict.

    Re-invokes the wrapped coroutine (which should open its own
    ``transaction()``) when it raises a conflict recognised by
    :func:`is_conflict_error`. Any other exception propagates immediately.

    Uses exponential backoff ``base_delay * backoff_factor ** attempt`` capped at
    ``max_delay``; with ``jitter`` the slept value is multiplied by a random
    factor in ``[0.5, 1.0]`` to avoid a thundering herd. Total attempts =
    ``max_retries + 1``. After the retries are exhausted, raises
    :class:`SurrealDbConflictError`.

    Args:
        max_retries: Number of retries after the first attempt (default 3).
        base_delay: Initial backoff in seconds (default 0.05).
        max_delay: Maximum backoff in seconds (default 2.0).
        backoff_factor: Exponential multiplier per retry (default 2.0).
        jitter: Randomise the backoff (default True). Set False for deterministic
            tests.

    Raises:
        ValueError: if any numeric parameter is out of range (at decoration time).

    Example::

        @retry_on_conflict(max_retries=5)
        async def transfer(src, dst, amount):
            async with SurrealDBConnectionManager.transaction() as tx:
                ...
    """
    if max_retries < 0:
        raise ValueError(f"max_retries must be >= 0, got {max_retries}")
    if base_delay <= 0:
        raise ValueError(f"base_delay must be > 0, got {base_delay}")
    if max_delay <= 0:
        raise ValueError(f"max_delay must be > 0, got {max_delay}")
    if backoff_factor <= 0:
        raise ValueError(f"backoff_factor must be > 0, got {backoff_factor}")

    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_error: BaseException | None = None
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as exc:
                    if not is_conflict_error(exc):
                        raise
                    last_error = exc
                    if attempt < max_retries:
                        delay = min(base_delay * (backoff_factor**attempt), max_delay)
                        if jitter:
                            delay *= random.uniform(0.5, 1.0)
                        logger.warning(
                            "Transaction conflict in %s (attempt %d/%d), retrying in %.3fs...",
                            func.__name__,
                            attempt + 1,
                            max_retries,
                            delay,
                        )
                        await asyncio.sleep(delay)
            raise SurrealDbConflictError(
                f"Transaction conflict persisted after {max_retries} retries in {func.__name__}: {last_error}"
            ) from last_error

        return wrapper  # type: ignore[return-value]

    return decorator
