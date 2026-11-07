"""The database clock, read by the tests that pin a moment (time travel, query order, embedded engine).

Kept out of the ``test_*.py`` modules so that none of them has to import another one: a cycle between test
modules only collects in one order.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from surreal_orm_lite import _client

#: Seconds :func:`now` waits before it reads the database clock.
CLOCK_SETTLE = 0.05


async def now(client: Any) -> datetime:
    """The database's clock, never the host's (spec §9), read :data:`CLOCK_SETTLE` seconds after the last write.

    A host clock that steps back a few milliseconds (WSL2 does, about every 30 s) makes SDK 3.0's embedded engine
    (SurrealDB 4.0) stamp a write just before the step later than a ``time::now()`` read right after it, and
    ``as_of()`` that moment then misses the write. The wait lets the read land after the write's stamp.
    """
    await asyncio.sleep(CLOCK_SETTLE)
    moment = await _client.query(client, "RETURN time::now();")
    assert isinstance(moment, datetime)
    return moment
