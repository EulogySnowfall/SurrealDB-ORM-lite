"""
Tests for model signals in SurrealDB-ORM-lite v0.4.0.

This module contains both unit tests (no database required) and
end-to-end tests (require a running SurrealDB instance) for:
- Signal class (pre/post signals)
- AroundSignal class (context manager-style signals)
- Integration with CRUD operations (save, update, merge, delete)
"""

import os

import pytest
from pydantic import Field
from surrealdb import RecordID

from src import surreal_orm_lite
from src.surreal_orm_lite.signals import (
    AroundSignal,
    Signal,
    around_delete,
    around_save,
    around_update,
    post_delete,
    post_save,
    post_update,
    pre_delete,
    pre_save,
    pre_update,
)

# =============================================================================
# Test Models
# =============================================================================


class SignalUser(surreal_orm_lite.BaseSurrealModel):
    """Test model for signal tests."""

    id: str | RecordID | None = None
    name: str = Field(..., max_length=100)
    email: str = Field(..., max_length=100)
    age: int = Field(default=25, ge=0)


# =============================================================================
# Unit Tests - Signal Class
# =============================================================================


class TestSignalClass:
    """Unit tests for the Signal class."""

    def test_signal_creation(self) -> None:
        """Signal should be created with a name."""
        sig = Signal("test_signal")
        assert sig.name == "test_signal"

    def test_connect_registers_handler(self) -> None:
        """connect() should register a handler for a model class."""
        sig = Signal("test")

        @sig.connect(SignalUser)
        async def handler(sender, **kwargs):  # type: ignore
            pass

        assert sig.has_handlers(SignalUser)

    def test_connect_prevents_duplicates(self) -> None:
        """connect() should not register the same handler twice."""
        sig = Signal("test")

        @sig.connect(SignalUser)
        async def handler(sender, **kwargs):  # type: ignore
            pass

        # Register same handler again
        sig.connect(SignalUser)(handler)

        assert len(sig._handlers[SignalUser]) == 1

    def test_disconnect_removes_handler(self) -> None:
        """disconnect() should remove a registered handler."""
        sig = Signal("test")

        @sig.connect(SignalUser)
        async def handler(sender, **kwargs):  # type: ignore
            pass

        assert sig.has_handlers(SignalUser)

        result = sig.disconnect(handler, SignalUser)
        assert result is True
        assert not sig.has_handlers(SignalUser)

    def test_disconnect_returns_false_for_unregistered(self) -> None:
        """disconnect() should return False if handler was not registered."""
        sig = Signal("test")

        async def handler(sender, **kwargs):  # type: ignore
            pass

        result = sig.disconnect(handler, SignalUser)
        assert result is False

    def test_has_handlers_returns_false_when_empty(self) -> None:
        """has_handlers() should return False when no handlers registered."""
        sig = Signal("test")
        assert not sig.has_handlers(SignalUser)

    def test_clear_specific_model(self) -> None:
        """clear() with model class should only clear that model's handlers."""
        sig = Signal("test")

        class OtherModel(surreal_orm_lite.BaseSurrealModel):
            id: str = Field(...)

        @sig.connect(SignalUser)
        async def handler1(sender, **kwargs):  # type: ignore
            pass

        @sig.connect(OtherModel)
        async def handler2(sender, **kwargs):  # type: ignore
            pass

        sig.clear(SignalUser)
        assert not sig.has_handlers(SignalUser)
        assert sig.has_handlers(OtherModel)

        # Cleanup
        sig.clear()

    def test_clear_all(self) -> None:
        """clear() without args should clear all handlers."""
        sig = Signal("test")

        @sig.connect(SignalUser)
        async def handler(sender, **kwargs):  # type: ignore
            pass

        sig.clear()
        assert not sig.has_handlers(SignalUser)

    async def test_send_calls_handlers(self) -> None:
        """send() should call all registered handlers."""
        sig = Signal("test")
        called = []

        @sig.connect(SignalUser)
        async def handler1(sender, **kwargs):  # type: ignore
            called.append("handler1")

        @sig.connect(SignalUser)
        async def handler2(sender, **kwargs):  # type: ignore
            called.append("handler2")

        await sig.send(SignalUser, instance="test_instance")

        assert called == ["handler1", "handler2"]

        # Cleanup
        sig.clear()

    async def test_send_passes_kwargs(self) -> None:
        """send() should pass kwargs to handlers."""
        sig = Signal("test")
        received_kwargs: dict = {}

        @sig.connect(SignalUser)
        async def handler(sender, **kwargs):  # type: ignore
            received_kwargs.update(kwargs)

        await sig.send(SignalUser, instance="test", created=True)

        assert received_kwargs["instance"] == "test"
        assert received_kwargs["created"] is True

        # Cleanup
        sig.clear()

    async def test_send_no_handlers(self) -> None:
        """send() should not raise when no handlers registered."""
        sig = Signal("test")
        await sig.send(SignalUser, instance="test")  # Should not raise

    async def test_send_handler_receives_sender(self) -> None:
        """Handlers should receive the sender class."""
        sig = Signal("test")
        received_sender = None

        @sig.connect(SignalUser)
        async def handler(sender, **kwargs):  # type: ignore
            nonlocal received_sender
            received_sender = sender

        await sig.send(SignalUser, instance="test")

        assert received_sender is SignalUser

        # Cleanup
        sig.clear()

    async def test_handlers_called_in_order(self) -> None:
        """Handlers should be called in registration order."""
        sig = Signal("test")
        order: list[int] = []

        @sig.connect(SignalUser)
        async def first(sender, **kwargs):  # type: ignore
            order.append(1)

        @sig.connect(SignalUser)
        async def second(sender, **kwargs):  # type: ignore
            order.append(2)

        @sig.connect(SignalUser)
        async def third(sender, **kwargs):  # type: ignore
            order.append(3)

        await sig.send(SignalUser, instance="test")

        assert order == [1, 2, 3]

        # Cleanup
        sig.clear()


# =============================================================================
# Unit Tests - AroundSignal Class
# =============================================================================


class TestAroundSignalClass:
    """Unit tests for the AroundSignal class."""

    def test_around_signal_creation(self) -> None:
        """AroundSignal should be created with a name."""
        sig = AroundSignal("test_around")
        assert sig.name == "test_around"

    def test_connect_registers_handler(self) -> None:
        """connect() should register a handler."""
        sig = AroundSignal("test")

        @sig.connect(SignalUser)
        async def handler(sender, **kwargs):  # type: ignore
            yield

        assert sig.has_handlers(SignalUser)

        # Cleanup
        sig.clear()

    def test_disconnect_removes_handler(self) -> None:
        """disconnect() should remove a handler."""
        sig = AroundSignal("test")

        @sig.connect(SignalUser)
        async def handler(sender, **kwargs):  # type: ignore
            yield

        result = sig.disconnect(handler, SignalUser)
        assert result is True
        assert not sig.has_handlers(SignalUser)

    def test_disconnect_returns_false_for_unregistered(self) -> None:
        """disconnect() should return False for unregistered handlers."""
        sig = AroundSignal("test")

        async def handler(sender, **kwargs):  # type: ignore
            yield

        result = sig.disconnect(handler, SignalUser)
        assert result is False

    def test_clear_all(self) -> None:
        """clear() should clear all handlers."""
        sig = AroundSignal("test")

        @sig.connect(SignalUser)
        async def handler(sender, **kwargs):  # type: ignore
            yield

        sig.clear()
        assert not sig.has_handlers(SignalUser)

    def test_clear_specific_model(self) -> None:
        """clear() with model class should only clear that model."""
        sig = AroundSignal("test")

        class OtherModel(surreal_orm_lite.BaseSurrealModel):
            id: str = Field(...)

        @sig.connect(SignalUser)
        async def handler1(sender, **kwargs):  # type: ignore
            yield

        @sig.connect(OtherModel)
        async def handler2(sender, **kwargs):  # type: ignore
            yield

        sig.clear(SignalUser)
        assert not sig.has_handlers(SignalUser)
        assert sig.has_handlers(OtherModel)

        # Cleanup
        sig.clear()

    async def test_wrap_executes_before_and_after(self) -> None:
        """wrap() should run pre-yield code before and post-yield code after."""
        sig = AroundSignal("test")
        order: list[str] = []

        @sig.connect(SignalUser)
        async def handler(sender, **kwargs):  # type: ignore
            order.append("before")
            yield
            order.append("after")

        async with sig.wrap(SignalUser, instance="test"):
            order.append("during")

        assert order == ["before", "during", "after"]

        # Cleanup
        sig.clear()

    async def test_wrap_no_handlers(self) -> None:
        """wrap() should work even with no handlers."""
        sig = AroundSignal("test")
        executed = False

        async with sig.wrap(SignalUser, instance="test"):
            executed = True

        assert executed

    async def test_wrap_multiple_handlers(self) -> None:
        """wrap() with multiple handlers should nest properly."""
        sig = AroundSignal("test")
        order: list[str] = []

        @sig.connect(SignalUser)
        async def handler1(sender, **kwargs):  # type: ignore
            order.append("h1_before")
            yield
            order.append("h1_after")

        @sig.connect(SignalUser)
        async def handler2(sender, **kwargs):  # type: ignore
            order.append("h2_before")
            yield
            order.append("h2_after")

        async with sig.wrap(SignalUser, instance="test"):
            order.append("during")

        assert order == ["h1_before", "h2_before", "during", "h2_after", "h1_after"]

        # Cleanup
        sig.clear()

    async def test_wrap_handler_no_yield(self) -> None:
        """wrap() should skip handlers that don't yield and log a warning."""
        sig = AroundSignal("test")
        order: list[str] = []

        @sig.connect(SignalUser)
        async def no_yield_handler(sender, **kwargs):  # type: ignore
            order.append("no_yield")
            # Async generator that returns before yielding; pre-yield side effects still run
            return
            yield  # type: ignore  # noqa: B901  # makes it an async generator

        async with sig.wrap(SignalUser, instance="test"):
            order.append("during")

        # The handler's pre-yield code ran, but it returned before yielding, so
        # wrap() skipped it for the "after" phase.
        assert order == ["no_yield", "during"]

        # Cleanup
        sig.clear()

    async def test_wrap_passes_kwargs(self) -> None:
        """wrap() should pass kwargs to handlers."""
        sig = AroundSignal("test")
        received_kwargs: dict = {}

        @sig.connect(SignalUser)
        async def handler(sender, **kwargs):  # type: ignore
            received_kwargs.update(kwargs)
            yield

        async with sig.wrap(SignalUser, instance="test", extra="value"):
            pass

        assert received_kwargs["instance"] == "test"
        assert received_kwargs["extra"] == "value"

        # Cleanup
        sig.clear()


# =============================================================================
# Unit Tests - Pre-defined Signal Instances
# =============================================================================


class TestPredefinedSignals:
    """Test that pre-defined signal instances exist and have correct names."""

    def test_pre_save_signal(self) -> None:
        assert isinstance(pre_save, Signal)
        assert pre_save.name == "pre_save"

    def test_post_save_signal(self) -> None:
        assert isinstance(post_save, Signal)
        assert post_save.name == "post_save"

    def test_pre_update_signal(self) -> None:
        assert isinstance(pre_update, Signal)
        assert pre_update.name == "pre_update"

    def test_post_update_signal(self) -> None:
        assert isinstance(post_update, Signal)
        assert post_update.name == "post_update"

    def test_pre_delete_signal(self) -> None:
        assert isinstance(pre_delete, Signal)
        assert pre_delete.name == "pre_delete"

    def test_post_delete_signal(self) -> None:
        assert isinstance(post_delete, Signal)
        assert post_delete.name == "post_delete"

    def test_around_save_signal(self) -> None:
        assert isinstance(around_save, AroundSignal)
        assert around_save.name == "around_save"

    def test_around_update_signal(self) -> None:
        assert isinstance(around_update, AroundSignal)
        assert around_update.name == "around_update"

    def test_around_delete_signal(self) -> None:
        assert isinstance(around_delete, AroundSignal)
        assert around_delete.name == "around_delete"


# =============================================================================
# Unit Tests - Import from package
# =============================================================================


class TestImports:
    """Test that signals can be imported from the main package."""

    def test_import_signals_from_package(self) -> None:
        """All signal instances should be importable from surreal_orm_lite."""
        from src.surreal_orm_lite import (
            AroundSignal,
            Signal,
            around_delete,
            around_save,
            around_update,
            post_delete,
            post_save,
            post_update,
            pre_delete,
            pre_save,
            pre_update,
        )

        assert Signal is not None
        assert AroundSignal is not None
        assert pre_save is not None
        assert post_save is not None
        assert pre_update is not None
        assert post_update is not None
        assert pre_delete is not None
        assert post_delete is not None
        assert around_save is not None
        assert around_update is not None
        assert around_delete is not None


# =============================================================================
# End-to-End Tests - Require SurrealDB
# =============================================================================

SURREALDB_HOST = os.environ.get("SURREALDB_HOST", "localhost")
SURREALDB_PORT = os.environ.get("SURREALDB_PORT", "8000")
SURREALDB_URL = f"http://{SURREALDB_HOST}:{SURREALDB_PORT}"
SURREALDB_USER = "root"
SURREALDB_PASS = "root"
SURREALDB_NAMESPACE = "ns"
SURREALDB_DATABASE = "db"


@pytest.fixture(scope="module", autouse=True)
def setup_surrealdb() -> None:
    """Setup SurrealDB connection for e2e tests."""
    surreal_orm_lite.SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )


@pytest.fixture(autouse=True)
async def cleanup_signals():
    """Clear all signal handlers before and after each test to avoid cross-contamination."""
    pre_save.clear(SignalUser)
    post_save.clear(SignalUser)
    pre_update.clear(SignalUser)
    post_update.clear(SignalUser)
    pre_delete.clear(SignalUser)
    post_delete.clear(SignalUser)
    around_save.clear(SignalUser)
    around_update.clear(SignalUser)
    around_delete.clear(SignalUser)

    yield

    pre_save.clear(SignalUser)
    post_save.clear(SignalUser)
    pre_update.clear(SignalUser)
    post_update.clear(SignalUser)
    pre_delete.clear(SignalUser)
    post_delete.clear(SignalUser)
    around_save.clear(SignalUser)
    around_update.clear(SignalUser)
    around_delete.clear(SignalUser)


@pytest.fixture
async def signal_user_data():
    """Create test user data and clean up after."""
    await SignalUser.objects().delete_table()

    await SignalUser(id="u1", name="Alice", email="alice@example.com", age=30).save()
    await SignalUser(id="u2", name="Bob", email="bob@example.com", age=25).save()

    yield

    await SignalUser.objects().delete_table()


# =============================================================================
# E2E Tests - Pre/Post Save Signals
# =============================================================================


class TestPreSaveE2E:
    """E2E tests for pre_save signal."""

    async def test_pre_save_fires_on_save(self) -> None:
        """pre_save should fire before save()."""
        events: list[dict] = []

        @pre_save.connect(SignalUser)
        async def on_pre_save(sender, instance, **kwargs):  # type: ignore
            events.append({"signal": "pre_save", "name": instance.name})

        user = SignalUser(id="ps1", name="PreSaveUser", email="pre@test.com")
        await user.save()

        assert len(events) == 1
        assert events[0]["signal"] == "pre_save"
        assert events[0]["name"] == "PreSaveUser"

        # Cleanup
        await SignalUser.objects().delete_table()

    async def test_pre_save_can_modify_instance(self) -> None:
        """pre_save handler can modify instance before saving."""

        @pre_save.connect(SignalUser)
        async def normalize_email(sender, instance, **kwargs):  # type: ignore
            object.__setattr__(instance, "email", instance.email.lower())

        user = SignalUser(id="ps2", name="Test", email="UPPER@TEST.COM")
        await user.save()

        fetched = await SignalUser.objects().get("ps2")
        assert fetched.email == "upper@test.com"

        # Cleanup
        await SignalUser.objects().delete_table()


class TestPostSaveE2E:
    """E2E tests for post_save signal."""

    async def test_post_save_fires_on_save(self) -> None:
        """post_save should fire after save()."""
        events: list[dict] = []

        @post_save.connect(SignalUser)
        async def on_post_save(sender, instance, created, **kwargs):  # type: ignore
            events.append(
                {
                    "signal": "post_save",
                    "name": instance.name,
                    "created": created,
                }
            )

        user = SignalUser(id="pst1", name="PostSaveUser", email="post@test.com")
        await user.save()

        assert len(events) == 1
        assert events[0]["signal"] == "post_save"
        assert events[0]["created"] is True

        # Cleanup
        await SignalUser.objects().delete_table()

    async def test_post_save_has_created_flag(self) -> None:
        """post_save should include created=True for new records."""
        created_flags: list[bool] = []

        @post_save.connect(SignalUser)
        async def track_created(sender, instance, created, **kwargs):  # type: ignore
            created_flags.append(created)

        user = SignalUser(id="pst2", name="CreatedUser", email="created@test.com")
        await user.save()

        assert created_flags == [True]

        # Cleanup
        await SignalUser.objects().delete_table()

    async def test_pre_and_post_save_order(self) -> None:
        """pre_save should fire before post_save."""
        order: list[str] = []

        @pre_save.connect(SignalUser)
        async def on_pre(sender, **kwargs):  # type: ignore
            order.append("pre_save")

        @post_save.connect(SignalUser)
        async def on_post(sender, **kwargs):  # type: ignore
            order.append("post_save")

        user = SignalUser(id="ord1", name="OrderTest", email="order@test.com")
        await user.save()

        assert order == ["pre_save", "post_save"]

        # Cleanup
        await SignalUser.objects().delete_table()


# =============================================================================
# E2E Tests - Pre/Post Update Signals
# =============================================================================


class TestPrePostUpdateE2E:
    """E2E tests for pre_update and post_update signals."""

    async def test_pre_update_fires_on_update(self, signal_user_data: None) -> None:
        """pre_update should fire before update()."""
        events: list[dict] = []

        @pre_update.connect(SignalUser)
        async def on_pre_update(sender, instance, update_fields, **kwargs):  # type: ignore
            events.append(
                {
                    "signal": "pre_update",
                    "fields": update_fields,
                }
            )

        user = await SignalUser.objects().get("u1")
        user.age = 31
        await user.update()

        assert len(events) == 1
        assert events[0]["signal"] == "pre_update"
        assert "age" in events[0]["fields"]

    async def test_post_update_fires_on_update(self, signal_user_data: None) -> None:
        """post_update should fire after update()."""
        events: list[dict] = []

        @post_update.connect(SignalUser)
        async def on_post_update(sender, instance, update_fields, **kwargs):  # type: ignore
            events.append(
                {
                    "signal": "post_update",
                    "fields": update_fields,
                }
            )

        user = await SignalUser.objects().get("u1")
        user.age = 35
        await user.update()

        assert len(events) == 1
        assert events[0]["signal"] == "post_update"

    async def test_update_signals_order(self, signal_user_data: None) -> None:
        """pre_update should fire before post_update."""
        order: list[str] = []

        @pre_update.connect(SignalUser)
        async def on_pre(sender, **kwargs):  # type: ignore
            order.append("pre_update")

        @post_update.connect(SignalUser)
        async def on_post(sender, **kwargs):  # type: ignore
            order.append("post_update")

        user = await SignalUser.objects().get("u1")
        user.name = "Updated"
        await user.update()

        assert order == ["pre_update", "post_update"]

    async def test_merge_fires_update_signals(self, signal_user_data: None) -> None:
        """merge() should also fire pre_update and post_update signals."""
        events: list[dict] = []

        @pre_update.connect(SignalUser)
        async def on_pre(sender, instance, update_fields, **kwargs):  # type: ignore
            events.append({"signal": "pre_update", "fields": update_fields})

        @post_update.connect(SignalUser)
        async def on_post(sender, instance, update_fields, **kwargs):  # type: ignore
            events.append({"signal": "post_update", "fields": update_fields})

        user = await SignalUser.objects().get("u1")
        await user.merge(age=40)

        assert len(events) == 2
        assert events[0]["signal"] == "pre_update"
        assert events[0]["fields"] == ["age"]
        assert events[1]["signal"] == "post_update"


# =============================================================================
# E2E Tests - Pre/Post Delete Signals
# =============================================================================


class TestPrePostDeleteE2E:
    """E2E tests for pre_delete and post_delete signals."""

    async def test_pre_delete_fires_on_delete(self) -> None:
        """pre_delete should fire before delete()."""
        events: list[dict] = []

        @pre_delete.connect(SignalUser)
        async def on_pre_delete(sender, instance, **kwargs):  # type: ignore
            events.append({"signal": "pre_delete", "name": instance.name})

        user = SignalUser(id="d1", name="DeleteMe", email="delete@test.com")
        await user.save()
        await user.delete()

        assert len(events) == 1
        assert events[0]["signal"] == "pre_delete"
        assert events[0]["name"] == "DeleteMe"

    async def test_post_delete_fires_on_delete(self) -> None:
        """post_delete should fire after delete()."""
        events: list[dict] = []

        @post_delete.connect(SignalUser)
        async def on_post_delete(sender, instance, **kwargs):  # type: ignore
            events.append({"signal": "post_delete", "name": instance.name})

        user = SignalUser(id="d2", name="DeleteMe2", email="delete2@test.com")
        await user.save()
        await user.delete()

        assert len(events) == 1
        assert events[0]["signal"] == "post_delete"

    async def test_delete_signals_order(self) -> None:
        """pre_delete should fire before post_delete."""
        order: list[str] = []

        @pre_delete.connect(SignalUser)
        async def on_pre(sender, **kwargs):  # type: ignore
            order.append("pre_delete")

        @post_delete.connect(SignalUser)
        async def on_post(sender, **kwargs):  # type: ignore
            order.append("post_delete")

        user = SignalUser(id="d3", name="OrderDelete", email="orderdelete@test.com")
        await user.save()
        await user.delete()

        assert order == ["pre_delete", "post_delete"]


# =============================================================================
# E2E Tests - Around Signals
# =============================================================================


class TestAroundSaveE2E:
    """E2E tests for around_save signal."""

    async def test_around_save_wraps_save(self) -> None:
        """around_save should wrap the save operation."""
        order: list[str] = []

        @around_save.connect(SignalUser)
        async def wrap_save(sender, instance, **kwargs):  # type: ignore
            order.append("before_save")
            yield
            order.append("after_save")

        user = SignalUser(id="as1", name="AroundSave", email="around@test.com")
        await user.save()

        assert order == ["before_save", "after_save"]

        # Verify save actually happened
        fetched = await SignalUser.objects().get("as1")
        assert fetched.name == "AroundSave"

        # Cleanup
        await SignalUser.objects().delete_table()

    async def test_around_save_with_pre_post(self) -> None:
        """around_save should work alongside pre_save and post_save."""
        order: list[str] = []

        @pre_save.connect(SignalUser)
        async def on_pre(sender, **kwargs):  # type: ignore
            order.append("pre_save")

        @around_save.connect(SignalUser)
        async def wrap_save(sender, **kwargs):  # type: ignore
            order.append("around_before")
            yield
            order.append("around_after")

        @post_save.connect(SignalUser)
        async def on_post(sender, **kwargs):  # type: ignore
            order.append("post_save")

        user = SignalUser(id="as2", name="AllSignals", email="all@test.com")
        await user.save()

        assert order == ["pre_save", "around_before", "around_after", "post_save"]

        # Cleanup
        await SignalUser.objects().delete_table()

    async def test_around_save_can_measure_time(self) -> None:
        """around_save can be used to measure operation duration."""
        import time

        durations: list[float] = []

        @around_save.connect(SignalUser)
        async def time_save(sender, instance, **kwargs):  # type: ignore
            start = time.time()
            yield
            durations.append(time.time() - start)

        user = SignalUser(id="as3", name="TimedSave", email="timed@test.com")
        await user.save()

        assert len(durations) == 1
        assert durations[0] >= 0

        # Cleanup
        await SignalUser.objects().delete_table()


class TestAroundUpdateE2E:
    """E2E tests for around_update signal."""

    async def test_around_update_wraps_update(self, signal_user_data: None) -> None:
        """around_update should wrap the update operation."""
        order: list[str] = []

        @around_update.connect(SignalUser)
        async def wrap_update(sender, instance, **kwargs):  # type: ignore
            order.append("before_update")
            yield
            order.append("after_update")

        user = await SignalUser.objects().get("u1")
        user.age = 99
        await user.update()

        assert order == ["before_update", "after_update"]

    async def test_around_update_wraps_merge(self, signal_user_data: None) -> None:
        """around_update should also wrap merge operations."""
        order: list[str] = []

        @around_update.connect(SignalUser)
        async def wrap_update(sender, instance, **kwargs):  # type: ignore
            order.append("before_merge")
            yield
            order.append("after_merge")

        user = await SignalUser.objects().get("u1")
        await user.merge(age=50)

        assert order == ["before_merge", "after_merge"]


class TestAroundDeleteE2E:
    """E2E tests for around_delete signal."""

    async def test_around_delete_wraps_delete(self) -> None:
        """around_delete should wrap the delete operation."""
        order: list[str] = []

        @around_delete.connect(SignalUser)
        async def wrap_delete(sender, instance, **kwargs):  # type: ignore
            order.append("before_delete")
            yield
            order.append("after_delete")

        user = SignalUser(id="ad1", name="AroundDelete", email="aroundel@test.com")
        await user.save()
        await user.delete()

        assert order == ["before_delete", "after_delete"]


# =============================================================================
# E2E Tests - Full Lifecycle
# =============================================================================


class TestFullLifecycle:
    """E2E tests for a full model lifecycle with all signals."""

    async def test_full_lifecycle_signals(self) -> None:
        """Test signals across create -> update -> delete lifecycle."""
        events: list[str] = []

        @pre_save.connect(SignalUser)
        async def on_pre_save(sender, **kwargs):  # type: ignore
            events.append("pre_save")

        @post_save.connect(SignalUser)
        async def on_post_save(sender, **kwargs):  # type: ignore
            events.append("post_save")

        @pre_update.connect(SignalUser)
        async def on_pre_update(sender, **kwargs):  # type: ignore
            events.append("pre_update")

        @post_update.connect(SignalUser)
        async def on_post_update(sender, **kwargs):  # type: ignore
            events.append("post_update")

        @pre_delete.connect(SignalUser)
        async def on_pre_delete(sender, **kwargs):  # type: ignore
            events.append("pre_delete")

        @post_delete.connect(SignalUser)
        async def on_post_delete(sender, **kwargs):  # type: ignore
            events.append("post_delete")

        # Create
        user = SignalUser(id="lc1", name="Lifecycle", email="lc@test.com")
        await user.save()

        # Update
        user.age = 50
        await user.update()

        # Delete
        await user.delete()

        assert events == [
            "pre_save",
            "post_save",
            "pre_update",
            "post_update",
            "pre_delete",
            "post_delete",
        ]

    async def test_no_signals_when_not_connected(self) -> None:
        """CRUD operations should work without any signals connected."""
        # No signals registered for SignalUser (cleanup fixture clears them)
        user = SignalUser(id="ns1", name="NoSignal", email="no@test.com")
        await user.save()

        fetched = await SignalUser.objects().get("ns1")
        assert fetched.name == "NoSignal"

        fetched.age = 50
        await fetched.update()

        await fetched.merge(age=60)

        await fetched.delete()

        # Should complete without errors


# =============================================================================
# E2E Tests - CRUD Regression
# =============================================================================


class TestCrudRegression:
    """Regression tests to ensure signals don't break existing CRUD behavior."""

    async def test_save_still_works(self) -> None:
        """save() should still work correctly with signals enabled."""
        user = SignalUser(id="reg1", name="RegSave", email="reg@test.com", age=28)
        await user.save()

        fetched = await SignalUser.objects().get("reg1")
        assert fetched.name == "RegSave"
        assert fetched.email == "reg@test.com"
        assert fetched.age == 28

        # Cleanup
        await SignalUser.objects().delete_table()

    async def test_update_still_works(self) -> None:
        """update() should still work correctly with signals enabled."""
        user = SignalUser(id="reg2", name="RegUpdate", email="regup@test.com", age=30)
        await user.save()

        user.name = "Updated"
        user.age = 31
        await user.update()

        fetched = await SignalUser.objects().get("reg2")
        assert fetched.name == "Updated"
        assert fetched.age == 31

        # Cleanup
        await SignalUser.objects().delete_table()

    async def test_merge_still_works(self) -> None:
        """merge() should still work correctly with signals enabled."""
        user = SignalUser(id="reg3", name="RegMerge", email="regmerge@test.com", age=25)
        await user.save()

        await user.merge(age=26)

        fetched = await SignalUser.objects().get("reg3")
        assert fetched.age == 26
        assert fetched.name == "RegMerge"

        # Cleanup
        await SignalUser.objects().delete_table()

    async def test_delete_still_works(self) -> None:
        """delete() should still work correctly with signals enabled."""
        user = SignalUser(id="reg4", name="RegDelete", email="regdel@test.com")
        await user.save()

        await user.delete()

        exists = await SignalUser.objects().filter(name="RegDelete").exists()
        assert exists is False

    async def test_save_duplicate_still_raises(self) -> None:
        """save() with duplicate ID should still raise SurrealDbError."""
        user1 = SignalUser(id="dup1", name="First", email="first@test.com")
        await user1.save()

        user2 = SignalUser(id="dup1", name="Duplicate", email="dup@test.com")
        with pytest.raises(surreal_orm_lite.SurrealDbError) as exc:
            await user2.save()

        assert "already exists" in str(exc.value)

        # Cleanup
        await SignalUser.objects().delete_table()

    async def test_update_without_id_still_raises(self) -> None:
        """update() without ID should still raise SurrealDbError."""
        user = SignalUser(name="NoId", email="noid@test.com")

        with pytest.raises(surreal_orm_lite.SurrealDbError) as exc:
            await user.update()

        assert "Can't update data, no id found." in str(exc.value)

    async def test_delete_without_id_still_raises(self) -> None:
        """delete() without ID should raise SurrealDbError before emitting signals."""
        user = SignalUser(name="NoId", email="noid@test.com")

        with pytest.raises(surreal_orm_lite.SurrealDbError) as exc:
            await user.delete()

        assert "Can't delete data, no id found." in str(exc.value)

    async def test_delete_nonexistent_still_raises(self) -> None:
        """delete() on non-existent record should still raise SurrealDbError."""
        user = SignalUser(id="nonexist999", name="Ghost", email="ghost@test.com")

        with pytest.raises(surreal_orm_lite.SurrealDbError) as exc:
            await user.delete()

        assert "not found" in str(exc.value)
