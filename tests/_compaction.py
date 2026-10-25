"""Wait for SurrealDB's background compaction of a vector index before a test writes to its table.

From SurrealDB 3.3 an HNSW index buffers the writes it receives and merges them through a background compaction that
starts right after them. While it runs, ``INFO FOR INDEX <index> ON <table>`` reports ``building.compacting = true``,
and a write to the table can fail with "Transaction conflict: Write conflict, retry the transaction" (issue #344).
Measured on 3.3.4: about 0.25 s after a seed of six records, over WebSocket and HTTP alike.

SurrealDB 2.7.0, 3.1.5 and the embedded engine report no ``compacting`` key, so the wait returns after one read and
adds no delay there. The wait is a read, never a write, and never retries a test's own write (``tests/_retry.py`` does):
a test calls it right after its seed, before any spy records the queries it sends.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from surreal_orm_lite import BaseSurrealModel

#: How long the wait gives a compaction before it fails the test: forty times the longest one measured.
COMPACTION_TIMEOUT = 10.0

#: The pause between two reads of the index's state.
_POLL_INTERVAL = 0.02

#: A ``DEFINE INDEX`` statement of a vector index: its name, then its table.
_VECTOR_INDEX = re.compile(
    r"DEFINE\s+INDEX\s+(?:OVERWRITE\s+|IF\s+NOT\s+EXISTS\s+)?`?(\w+)`?\s+ON\s+(?:TABLE\s+)?`?(\w+)`?\s+"
    r"(?:FIELDS|COLUMNS)\s+[^;]*?\b(?:HNSW|DISKANN)\b",
    re.IGNORECASE,
)


async def wait_for_compaction(client: Any, index: str, table: str, *, timeout: float = COMPACTION_TIMEOUT) -> None:
    """Return once ``INFO FOR INDEX`` *index* ``ON`` *table* no longer reports ``building.compacting = true``.

    *client* is the SDK client the test seeded through; *index* and *table* are constant names of the test. An engine
    that reports no ``compacting`` key (SurrealDB 2.x, 3.1, the embedded engine) returns after the first read. A
    compaction still running after *timeout* seconds fails the test with ``AssertionError``, and so does an answer
    without a ``building`` state or an index that does not exist (the server's own error): the wait never passes
    silently.
    """
    statement = f"INFO FOR INDEX {index} ON {table};"
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        info = await client.query(statement, {})
        building = info.get("building") if isinstance(info, dict) else None
        if not isinstance(building, dict):
            raise AssertionError(f"{statement} answered no building state: {info!r}")
        if building.get("compacting") is not True:
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                f"{statement} still reports building.compacting = true after {timeout} s: the background compaction "
                "of the vector index did not finish"
            )
        await asyncio.sleep(_POLL_INTERVAL)


async def wait_for_model_indexes(client: Any, model: type[BaseSurrealModel]) -> None:
    """:func:`wait_for_compaction` on each vector index *model* declares (``get_indexes()``), on its table."""
    table = model.get_table_name()
    for index in model.get_indexes():
        if index.hnsw:
            await wait_for_compaction(client, index.name, table)


async def wait_for_defined_indexes(client: Any, ddl: str) -> None:
    """:func:`wait_for_compaction` on each vector index (``HNSW`` or ``DISKANN``) that a ``DEFINE INDEX`` of *ddl*, a
    test's hand-written statements, defines. A *ddl* that defines none fails the test: a wait on nothing would pass
    silently."""
    defined = _VECTOR_INDEX.findall(ddl)
    if not defined:
        raise AssertionError(f"no vector index is defined in {ddl!r}")
    for index, table in defined:
        await wait_for_compaction(client, index, table)
