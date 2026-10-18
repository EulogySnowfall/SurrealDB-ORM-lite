"""The settings of one named connection (v0.29.0), as ``SurrealDBConnectionManager.get_config()`` returns them."""

import dataclasses

__all__ = ["ConnectionConfig"]


@dataclasses.dataclass(frozen=True)
class ConnectionConfig:
    """Immutable settings of one named connection (v0.29.0), as returned by ``get_config()``.

    The full SurrealDB-ORM's dataclass, in its field order and built the same ways (by position or by keyword),
    without its ``protocol`` field (the official SDK 2.0 speaks CBOR only). ``user`` and ``password`` may be
    ``None`` (the embedded engine needs neither) but are never left out. The password is excluded from ``repr``;
    ``dataclasses.asdict()`` includes it — do not log that.

    ::

        ConnectionConfig("mem://", None, None, "test", "test")
        ConnectionConfig(url="ws://db:8000/rpc", user="root", password="…", namespace="shop", database="main")
    """

    url: str
    user: str | None
    password: str | None = dataclasses.field(repr=False)
    namespace: str
    database: str
