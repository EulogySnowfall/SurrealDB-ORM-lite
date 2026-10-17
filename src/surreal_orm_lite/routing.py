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


def check_config_connection(cls: type, value: object) -> None:
    """R7: the ``connection`` key of a model's ``SurrealConfigDict``, checked when the class is defined.

    ``None`` (the default connection) is not passed here. Whether the name is registered is checked at the model's
    first database call (R2), never here: a model may be defined before its connection is added.

    :raises TypeError: *value* is not a ``str``.
    :raises ValueError: *value* is empty.
    """
    if not isinstance(value, str):
        raise TypeError(f"{cls.__name__}: connection must be a str, got {type(value).__name__}.")
    if not value:
        raise ValueError(f"{cls.__name__}: connection must not be empty.")
