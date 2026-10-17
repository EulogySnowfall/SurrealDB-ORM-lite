"""Connection names and the checks of multi-database routing (v0.29.0).

A leaf module: it imports nothing of the ORM but its exceptions, so the connection manager, the models and the
querysets can all use it. A connection name is never written into SurrealQL: it is a dictionary key, and a value
quoted with ``!r`` in error messages.

A transaction belongs to the connection it was opened on: ``SurrealDBConnectionManager.transaction()`` tags it with
that connection's name. The guards read the tag with ``inspect.getattr_static`` only — the instance ``__dict__``,
then the class — and before anything else: no ``__getattr__``, no property and no descriptor of the object runs, so a
transaction is recognised without asking it, as the query cache's ``writing()`` recognises one by its class.
"""

import inspect

from .exceptions import SurrealDbError


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


def transaction_connection(tx: object) -> str | None:
    """The name of the connection *tx* was opened on, or ``None`` when it carries no such tag.

    ``None`` for no transaction, a transaction the manager did not tag (one built by hand), and any other object.
    The tag is read statically (see the module docstring): the object itself is never asked.
    """
    if tx is None:
        return None
    tag = inspect.getattr_static(tx, "_connection_name", None)
    return tag if isinstance(tag, str) else None


def _connection_of(model: type) -> str | None:
    """*model*'s connection name right now (its ``get_connection_name()``), read without importing the models."""
    resolve = getattr(model, "get_connection_name", None)
    name = resolve() if callable(resolve) else None
    return name if isinstance(name, str) else None


def check_transaction(model: type, tx: object, operation: str) -> None:
    """X1: refuse *operation* of *model* inside a transaction of another connection, before any request or signal.

    The transaction's tag is read first, and statically (:func:`transaction_connection`); a transaction without one
    returns at once. Only then is *model*'s connection resolved — by name, never through the registry, so this never
    raises an unknown-connection error in place of its own. *operation* names the call as the user wrote it, such as
    ``"Event.save()"`` or ``"Event.objects(tx=)"``.

    :raises SurrealDbError: *model* runs on another connection than the one *tx* was opened on.
    """
    tx_name = transaction_connection(tx)
    if tx_name is None:
        return
    model_name = _connection_of(model)
    if model_name is None or model_name == tx_name:
        return
    raise SurrealDbError(
        f"{operation} cannot run in this transaction: the transaction belongs to connection {tx_name!r} and "
        f"{model.__name__} runs on connection {model_name!r}. A transaction is one connection's (one database): "
        f"open it on {model_name!r} — SurrealDBConnectionManager.transaction({model_name!r}) — or run the block "
        f"inside using({tx_name!r})."
    )


def check_call_transaction(name: str, tx: object) -> None:
    """X1m: refuse ``SurrealDBConnectionManager.call_function(tx=)`` on connection *name* with a transaction of another.

    The tag is read statically, as in :func:`check_transaction`.

    :raises SurrealDbError: *tx* was opened on another connection than *name*.
    """
    tx_name = transaction_connection(tx)
    if tx_name is None or tx_name == name:
        return
    raise SurrealDbError(
        f"call_function() cannot run in this transaction: the transaction belongs to connection {tx_name!r} and the "
        f"call runs on connection {name!r}. A transaction is one connection's (one database): call it inside "
        f"using({tx_name!r}), or open the transaction on {name!r}."
    )
