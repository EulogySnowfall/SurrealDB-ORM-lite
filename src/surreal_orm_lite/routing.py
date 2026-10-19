"""Connection names and the checks of multi-database routing (v0.29.0).

A leaf module: it imports nothing of the ORM but its exceptions, so the connection manager, the models and the
querysets can all use it. A connection name is never written into SurrealQL: it is a dictionary key, and a value
quoted with ``!r`` in error messages.

A transaction belongs to the connection it was opened on: ``SurrealDBConnectionManager.transaction()`` tags it with
that connection's name. The guards read the tag with ``inspect.getattr_static`` only — the instance ``__dict__``,
then the class — and before anything else: no ``__getattr__``, no property and no descriptor of the object runs, so a
transaction is recognised without asking it, as the query cache's ``writing()`` recognises one by its class.

One statement runs in one database, so it cannot involve two connections: a subquery of a model of another
connection, a relation between records or edge models of two connections, a link followed through a field typed with
a model of another connection (README section 30, "Multiple databases"). The guards compare connection **names**
(``get_connection_name()``), never connection classes: they never look the registry up, so a guard never raises an
unknown-connection error in place of its own. What a field links to is read from its annotation
(:func:`linked_models`), duck-typed so this module does not import the models.
"""

import collections.abc
import functools
import inspect
import types
import typing
from collections.abc import Iterable, Iterator
from typing import Final

from .exceptions import SurrealDbError

#: The origins of a union: ``Optional[X]`` and ``Union[X, Y]``, and ``X | Y`` (the same type from Python 3.14 on).
_UNION_ORIGINS: Final = frozenset({typing.Union, types.UnionType})
#: The generic origins whose arguments are the items of a collection of links (``list[Tag]``, ``tuple[Tag, ...]``).
_ITEM_ORIGINS: Final = frozenset(
    {
        list,
        set,
        frozenset,
        tuple,
        collections.abc.Sequence,
        collections.abc.MutableSequence,
        collections.abc.Set,
        collections.abc.MutableSet,
    }
)
#: The generic origins whose last argument is the value of a mapping of links (``dict[str, Author]``).
_MAPPING_ORIGINS: Final = frozenset({dict, collections.abc.Mapping, collections.abc.MutableMapping})


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
    """Check the ``connection`` key of a model's ``SurrealConfigDict`` when the class is defined.

    ``None`` (the default connection) is not passed here. Whether the name is registered is checked at the model's
    first database call, never here: a model may be defined before its connection is added.

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
    """Refuse *operation* of *model* inside a transaction of another connection, before any request or signal.

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
    """Refuse ``SurrealDBConnectionManager.call_function(tx=)`` on connection *name* with a transaction of another.

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


def check_subquery(outer: type, inner: type) -> None:
    """Refuse a subquery of *inner* inside a statement of *outer* when they run on two connections.

    *outer* is the model of the statement the subquery is sent in — the outermost one, whatever the depth: SurrealDB
    runs every subquery of a statement in that statement's database.

    :raises SurrealDbError: *inner* runs on another connection than *outer*.
    """
    outer_name = _connection_of(outer)
    inner_name = _connection_of(inner)
    if outer_name is None or inner_name is None or outer_name == inner_name:
        return
    raise SurrealDbError(
        f"{inner.__name__} (connection {inner_name!r}) cannot be read in a subquery of {outer.__name__} (connection "
        f"{outer_name!r}): SurrealDB runs a subquery in the database of the statement around it. Read "
        f"{inner.__name__} first through its own connection and filter on its values, or route both models to one "
        "connection."
    )


def check_same_connection(operation: str, first: type, *others: type) -> None:
    """Refuse *operation* (``"Event.relate()"``…) when one of *others* runs on another connection than *first*.

    *first* is the source record's model; *others* the target's model, the edge model and ``model_class``, in that
    order (a ``RecordID`` or a ``"table:id"`` target names no model and is not passed). The first model of another
    connection is named.

    :raises SurrealDbError: two connections.
    """
    first_name = _connection_of(first)
    if first_name is None:
        return
    for other in others:
        other_name = _connection_of(other)
        if other_name is None or other_name == first_name:
            continue
        raise SurrealDbError(
            f"{operation} cannot relate records of two connections: {first.__name__} runs on connection "
            f"{first_name!r} and {other.__name__} on connection {other_name!r}. A relation is stored in one database: "
            "route the models (and the edge model) to one connection."
        )


def _models_in(annotation: object) -> Iterator[type]:
    """The model classes *annotation* names: itself, or through a union, ``Annotated``, a collection's items and a
    mapping's values. A forward reference (a string, a ``ForwardRef``) names none."""
    origin = typing.get_origin(annotation)
    if origin is None:
        if isinstance(annotation, type) and callable(getattr(annotation, "get_connection_name", None)):
            yield annotation
        return
    arguments = typing.get_args(annotation)
    if origin is typing.Annotated:
        yield from _models_in(arguments[0])
    elif origin in _UNION_ORIGINS or origin in _ITEM_ORIGINS:
        for argument in arguments:
            yield from _models_in(argument)
    elif origin in _MAPPING_ORIGINS and arguments:
        yield from _models_in(arguments[-1])


def _linked_models(model: type, column: str) -> tuple[type, ...]:
    """:func:`linked_models`, computed."""
    to_py_field = getattr(model, "to_py_field", None)
    fields = getattr(model, "model_fields", None)
    if not callable(to_py_field) or not isinstance(fields, collections.abc.Mapping):
        return ()
    info = fields.get(to_py_field(column))
    if info is None:
        return ()
    return tuple(dict.fromkeys(_models_in(getattr(info, "annotation", None))))


@functools.lru_cache(maxsize=1024)
def _linked_models_of(model: type, column: str) -> tuple[type, ...]:
    """:func:`linked_models` of a complete model, cached per ``(model, column)``."""
    return _linked_models(model, column)


def linked_models(model: type, column: str) -> tuple[type, ...]:
    """The models the field stored under *column* links to: the model classes its annotation names.

    ``author: Author | RecordID | None`` gives ``(Author,)``, ``tags: list[Tag]`` ``(Tag,)``; unions,
    ``Optional``, ``Annotated``, collections and mapping values are walked. A field typed ``RecordID``, ``str`` or
    ``dict``, a nested Pydantic object, an unknown column and a forward reference give ``()``. Cached once *model*
    is complete; while it is not (``__pydantic_complete__`` false: an annotation still a forward reference), the
    result is computed and not cached, so a ``model_rebuild()`` that resolves the reference is seen at the next
    check. Only the classes are cached, never their connection.
    """
    if getattr(model, "__pydantic_complete__", False):
        return _linked_models_of(model, column)
    return _linked_models(model, column)


def check_links(model: type, columns: Iterable[str]) -> None:
    """Refuse a statement of *model* that follows a link to a model of another connection.

    *columns* are the columns the statement follows links through: its ``fetch()`` paths and the heads of the dotted
    paths of its other clauses. A dotted column is checked through its first segment (``author.name``: ``author``).

    :raises SurrealDbError: the first link to a model of another connection.
    """
    model_name: str | None = None
    for column in columns:
        head = column.partition(".")[0]
        for linked in linked_models(model, head):
            if model_name is None:
                model_name = _connection_of(model)
            linked_name = _connection_of(linked)
            if model_name is None or linked_name is None or linked_name == model_name:
                continue
            field = model.to_py_field(head)  # type: ignore[attr-defined]  # linked_models() found it
            raise SurrealDbError(
                f"{model.__name__}.{field} links to {linked.__name__}, which runs on connection {linked_name!r}, while "
                f"{model.__name__} runs on connection {model_name!r}: SurrealDB resolves a record link in the "
                "database of the statement, so following it would read the wrong database. Read "
                f"{linked.__name__} through its own connection, or route both models to one connection."
            )
