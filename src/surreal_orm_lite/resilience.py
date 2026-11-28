"""Connection resilience (v0.51.0): how a WebSocket connection reconnects after its socket drops.

:class:`ReconnectPolicy` is the official SDK's ``ReconnectOptions`` under the ORM's names. It is given per connection,
to :meth:`SurrealDBConnectionManager.set_connection` or :meth:`~SurrealDBConnectionManager.add_connection`
(``reconnect=``), and read back from :meth:`~SurrealDBConnectionManager.get_config`.
"""

import dataclasses

from ._sdk import ReconnectOptions

__all__ = ["ReconnectPolicy"]


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
