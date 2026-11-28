"""The settings of one named connection (v0.29.0), as ``SurrealDBConnectionManager.get_config()`` returns them."""

import dataclasses

from .resilience import ReconnectPolicy

__all__ = ["ConnectionConfig"]


@dataclasses.dataclass(frozen=True)
class ConnectionConfig:
    """Immutable settings of one named connection (v0.29.0), as returned by ``get_config()``.

    The full SurrealDB-ORM's dataclass, in its field order and built the same ways (by position or by keyword),
    without its ``protocol`` field (the official SDK 2.0 speaks CBOR only). ``user`` and ``password`` may be
    ``None`` (the embedded engine needs neither) but are never left out. The password is excluded from ``repr``;
    ``dataclasses.asdict()`` includes it — do not log that.

    v0.51.0 appends ``reconnect`` (``True``, ``False`` or a :class:`ReconnectPolicy`) and ``renew_access``, as given to
    ``set_connection()``/``add_connection()``; both default to ``True``, so every earlier construction still works.

    ::

        ConnectionConfig("mem://", None, None, "test", "test")
        ConnectionConfig(url="ws://db:8000/rpc", user="root", password="…", namespace="shop", database="main")
    """

    url: str
    user: str | None
    password: str | None = dataclasses.field(repr=False)
    namespace: str
    database: str
    reconnect: bool | ReconnectPolicy = True
    renew_access: bool = True
