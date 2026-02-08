"""
Signal system for SurrealDB-ORM-lite.

Provides Django-style pre/post signals and around signals
for model lifecycle events (save, update, delete).

Usage:
    from surreal_orm_lite import pre_save, post_save, pre_delete, post_delete

    @post_save.connect(User)
    async def on_user_saved(sender, instance, created, **kwargs):
        if created:
            await send_welcome_email(instance.email)

    @around_save.connect(User)
    async def time_user_save(sender, instance, **kwargs):
        import time
        start = time.time()
        yield  # save() executes here
        duration = time.time() - start
        print(f"Save took {duration:.3f}s")
"""

from __future__ import annotations

import contextlib
import logging
from collections import defaultdict
from collections.abc import AsyncGenerator, Callable, Coroutine
from typing import Any

logger = logging.getLogger(__name__)

# Type alias for signal handlers
SignalHandler = Callable[..., Coroutine[Any, Any, None]]
AroundHandler = Callable[..., AsyncGenerator[None, None]]


class Signal:
    """
    A signal that dispatches events to registered async handlers.

    Handlers are registered per model class and called in registration order
    when the signal is sent.

    Example:
        pre_save = Signal("pre_save")

        @pre_save.connect(User)
        async def on_user_pre_save(sender, instance, **kwargs):
            print(f"About to save {instance}")
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._handlers: dict[type, list[SignalHandler]] = defaultdict(list)

    def connect(self, model_class: type) -> Callable[[SignalHandler], SignalHandler]:
        """
        Decorator to register a handler for a specific model class.

        Args:
            model_class: The model class to listen for signals on.

        Returns:
            Decorator that registers the handler.
        """

        def decorator(handler: SignalHandler) -> SignalHandler:
            if handler not in self._handlers[model_class]:
                self._handlers[model_class].append(handler)
            return handler

        return decorator

    def disconnect(self, handler: SignalHandler, model_class: type) -> bool:
        """
        Remove a handler for a specific model class.

        Args:
            handler: The handler function to remove.
            model_class: The model class the handler was registered for.

        Returns:
            True if the handler was found and removed, False otherwise.
        """
        handlers = self._handlers.get(model_class, [])
        if handler in handlers:
            handlers.remove(handler)
            return True
        return False

    async def send(self, sender: type, **kwargs: Any) -> None:
        """
        Dispatch the signal to all registered handlers for the sender class.

        Args:
            sender: The model class that is sending the signal.
            **kwargs: Additional keyword arguments passed to handlers.
        """
        for handler in self._handlers.get(sender, []):
            await handler(sender=sender, **kwargs)

    def has_handlers(self, model_class: type) -> bool:
        """Check if any handlers are registered for the given model class."""
        return bool(self._handlers.get(model_class))

    def clear(self, model_class: type | None = None) -> None:
        """
        Clear registered handlers.

        Args:
            model_class: If provided, clear handlers only for this model.
                        If None, clear all handlers for all models.
        """
        if model_class is not None:
            self._handlers.pop(model_class, None)
        else:
            self._handlers.clear()


class AroundSignal:
    """
    A signal that wraps an operation using async generators (context manager style).

    Handlers yield once: code before yield runs before the operation,
    code after yield runs after. This allows measuring timing, wrapping
    in try/except, etc.

    Example:
        around_save = AroundSignal("around_save")

        @around_save.connect(User)
        async def time_user_save(sender, instance, **kwargs):
            start = time.time()
            yield  # save() executes here
            duration = time.time() - start
            print(f"Save took {duration:.3f}s")
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._handlers: dict[type, list[AroundHandler]] = defaultdict(list)

    def connect(self, model_class: type) -> Callable[[AroundHandler], AroundHandler]:
        """
        Decorator to register an around handler for a specific model class.

        Args:
            model_class: The model class to listen for signals on.

        Returns:
            Decorator that registers the handler.
        """

        def decorator(handler: AroundHandler) -> AroundHandler:
            if handler not in self._handlers[model_class]:
                self._handlers[model_class].append(handler)
            return handler

        return decorator

    def disconnect(self, handler: AroundHandler, model_class: type) -> bool:
        """
        Remove a handler for a specific model class.

        Args:
            handler: The handler function to remove.
            model_class: The model class the handler was registered for.

        Returns:
            True if the handler was found and removed, False otherwise.
        """
        handlers = self._handlers.get(model_class, [])
        if handler in handlers:
            handlers.remove(handler)
            return True
        return False

    @contextlib.asynccontextmanager
    async def wrap(self, sender: type, **kwargs: Any) -> AsyncGenerator[None, None]:
        """
        Context manager that runs all around handlers for the sender class.

        The handlers' pre-yield code runs before yielding,
        and their post-yield code runs after in reverse (LIFO) order,
        similar to nested context managers.

        Args:
            sender: The model class that is sending the signal.
            **kwargs: Additional keyword arguments passed to handlers.
        """
        handlers = self._handlers.get(sender, [])
        generators: list[AsyncGenerator[None, None]] = []

        # Start all generators (run pre-yield code)
        try:
            for handler in handlers:
                gen = handler(sender=sender, **kwargs)
                try:
                    await gen.__anext__()
                except StopAsyncIteration:
                    logger.warning(
                        "AroundSignal handler %r for sender %r did not yield; it must yield exactly once.",
                        handler,
                        sender,
                    )
                    with contextlib.suppress(Exception):
                        await gen.aclose()
                    continue
                generators.append(gen)
        except Exception:
            # If startup fails, close any already-started generators in LIFO order
            for gen in reversed(generators):
                with contextlib.suppress(Exception):
                    await gen.aclose()
            raise

        try:
            yield
        finally:
            # Complete all generators (run post-yield code) in reverse (LIFO) order
            for gen in reversed(generators):
                try:
                    await gen.__anext__()
                except StopAsyncIteration:
                    continue
                else:
                    logger.warning(
                        "AroundSignal handler for sender %r yielded more than once; handlers must yield exactly once.",
                        sender,
                    )
                    with contextlib.suppress(Exception):
                        await gen.aclose()

    def has_handlers(self, model_class: type) -> bool:
        """Check if any handlers are registered for the given model class."""
        return bool(self._handlers.get(model_class))

    def clear(self, model_class: type | None = None) -> None:
        """
        Clear registered handlers.

        Args:
            model_class: If provided, clear handlers only for this model.
                        If None, clear all handlers for all models.
        """
        if model_class is not None:
            self._handlers.pop(model_class, None)
        else:
            self._handlers.clear()


# =============================================================================
# Pre-defined signal instances
# =============================================================================

# Pre/Post signals for save operations
pre_save = Signal("pre_save")
post_save = Signal("post_save")

# Pre/Post signals for update operations
pre_update = Signal("pre_update")
post_update = Signal("post_update")

# Pre/Post signals for delete operations
pre_delete = Signal("pre_delete")
post_delete = Signal("post_delete")

# Around signals
around_save = AroundSignal("around_save")
around_update = AroundSignal("around_update")
around_delete = AroundSignal("around_delete")
