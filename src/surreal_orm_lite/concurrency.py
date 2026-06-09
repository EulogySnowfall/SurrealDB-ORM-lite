# src/surreal_orm_lite/concurrency.py
"""Optimistic-concurrency helpers: conflict detection + retry decorator.

SurrealDB rolls a transaction back with a *retryable* error when a concurrent
writer modified the same data. It marks such errors with the phrase
"This transaction can be retried". ``retry_on_conflict`` re-runs the wrapped
coroutine when (and only when) ``is_conflict_error`` recognises that marker.
"""

from __future__ import annotations

import logging

from .exceptions import SurrealDbConflictError

logger = logging.getLogger(__name__)

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
