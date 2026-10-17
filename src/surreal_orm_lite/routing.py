"""Connection names and the checks of multi-database routing (v0.29.0).

A leaf module: it imports nothing of the ORM but its exceptions, so the connection manager, the models and the
querysets can all use it. A connection name is never written into SurrealQL: it is a dictionary key, and a value
quoted with ``!r`` in error messages.
"""


def check_name(name: object) -> str:
    """Return *name* when it is a usable connection name.

    :raises TypeError: *name* is not a ``str``.
    :raises ValueError: *name* is empty.
    """
    if not isinstance(name, str):
        raise TypeError(f"Connection name must be a str, not {type(name).__name__}.")
    if not name:
        raise ValueError("Connection name must not be empty.")
    return name
