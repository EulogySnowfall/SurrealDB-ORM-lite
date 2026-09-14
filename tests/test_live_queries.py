"""Tests for v0.19.0 — live queries (base): ``live()``, ``subscribe_live()``, ``kill()``.

Live queries work on **both** supported SurrealDB lines, so almost nothing here is
version-gated. The two measured divergences each get both branches, probed at runtime:

* a live query on a table that does not exist raises on 3.x and succeeds on 2.6.x;
* after ``kill()``, 3.x sends a ``KILLED`` notification and 2.6.x sends nothing — the ORM
  makes both terminate the stream, and the tests assert termination, not the mechanism.

Every test that consumes a stream does so inside ``asyncio.wait_for``: a live query that never
terminates would otherwise hang the whole suite instead of failing one test.
"""

import asyncio
from enum import StrEnum
from typing import Any

import pytest

from surreal_orm_lite import BaseSurrealModel
from surreal_orm_lite.enum import LiveAction


class _FakeClient:
    """Stand-in for the SDK connection: only ``live_queues`` matters to the registry."""

    def __init__(self) -> None:
        self.live_queues: dict[str, list[asyncio.Queue[Any]]] = {}


class Watched(BaseSurrealModel):
    id: str
    name: str = ""


# ==================== Task 1 — LiveAction ====================


class TestLiveAction:
    def test_is_a_str_enum(self) -> None:
        assert issubclass(LiveAction, StrEnum)

    def test_members_equal_the_raw_server_strings(self) -> None:
        assert LiveAction.CREATE == "CREATE"
        assert LiveAction.UPDATE == "UPDATE"
        assert LiveAction.DELETE == "DELETE"

    def test_carries_the_3x_killed_action(self) -> None:
        assert LiveAction.KILLED == "KILLED"


# ==================== Task 1 — subscriber registry ====================


class TestSubscriberRegistry:
    def test_register_returns_a_queue_visible_to_both_registries(self) -> None:
        from surreal_orm_lite import live

        client = _FakeClient()
        queue = live.register_subscriber(client, "abc")
        assert queue in client.live_queues["abc"]
        assert queue in live._SUBSCRIBERS["abc"]
        live.close_subscribers("abc")

    def test_close_pushes_the_sentinel_to_every_queue(self) -> None:
        from surreal_orm_lite import live

        client = _FakeClient()
        first = live.register_subscriber(client, "abc")
        second = live.register_subscriber(client, "abc")
        live.close_subscribers("abc")
        assert first.get_nowait() is live._STREAM_END
        assert second.get_nowait() is live._STREAM_END

    def test_close_drops_the_entry_and_is_idempotent(self) -> None:
        from surreal_orm_lite import live

        client = _FakeClient()
        live.register_subscriber(client, "abc")
        live.close_subscribers("abc")
        assert "abc" not in live._SUBSCRIBERS
        live.close_subscribers("abc")  # must not raise

    def test_unregister_leaves_the_other_subscribers_intact(self) -> None:
        from surreal_orm_lite import live

        client = _FakeClient()
        first = live.register_subscriber(client, "abc")
        second = live.register_subscriber(client, "abc")
        live.unregister_subscriber(client, "abc", first)
        assert live._SUBSCRIBERS["abc"] == [second]
        assert client.live_queues["abc"] == [second]
        live.close_subscribers("abc")

    def test_unregister_of_the_last_queue_drops_the_entry(self) -> None:
        from surreal_orm_lite import live

        client = _FakeClient()
        only = live.register_subscriber(client, "abc")
        live.unregister_subscriber(client, "abc", only)
        assert "abc" not in live._SUBSCRIBERS

    def test_registry_tolerates_a_client_without_live_queues(self) -> None:
        """A future SDK layout must degrade, not explode."""
        from surreal_orm_lite import live

        queue = live.register_subscriber(object(), "abc")
        assert queue in live._SUBSCRIBERS["abc"]
        live.close_subscribers("abc")

    def test_accepts_a_uuid_as_well_as_a_string(self) -> None:
        from uuid import uuid4

        from surreal_orm_lite import live

        client = _FakeClient()
        key = uuid4()
        queue = live.register_subscriber(client, key)
        assert queue in client.live_queues[str(key)]
        live.close_subscribers(key)
        assert str(key) not in live._SUBSCRIBERS


@pytest.fixture(autouse=True)
def _clear_registry() -> Any:
    from surreal_orm_lite import live

    yield
    live._SUBSCRIBERS.clear()
