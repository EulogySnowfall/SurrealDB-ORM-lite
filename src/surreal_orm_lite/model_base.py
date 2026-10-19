import contextlib
import copy
import functools
import logging
import os
import typing
import warnings
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from types import UnionType
from typing import TYPE_CHECKING, Any, Self, TypeVar, overload

import pydantic
from pydantic import BaseModel, ConfigDict, ModelWrapValidatorHandler, PrivateAttr, TypeAdapter, model_validator
from pydantic_core import ValidationError

from . import cache as _cache
from . import encoding as _encoding
from . import geo as _geo
from . import time_travel as _time_travel
from ._sdk import NotFoundError, RecordID, SdkDuration, ServerError
from .connection_manager import SurrealDBConnectionManager
from .exceptions import SurrealDbError
from .functions import SurrealFunc, _ComputedDefault
from .routing import check_config_connection, check_same_connection, check_transaction
from .schema import (
    Event,
    check_events_config,
    ensure_async_events_supported,
    event_subject,
    remove_event_statement,
)
from .signals import (
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
from .transaction import Transaction
from .types import Duration, _duration_from_sdk, _timedelta_from_sdk
from .utils import (
    PLAIN_CONTENT,
    VALID_ALIAS_PATTERN,
    apply_ddl_statements,
    build_set_clause,
    coerce_endpoint,
    format_record_id,
    merge_extra_vars,
    replace_content,
    user_stacklevel,
    validate_alias_name,
    validate_edge_name,
    validate_field_name,
    validate_graph_path,
    validate_patch_operations,
    validate_thing,
    warn_on_multiple_statements,
)

if TYPE_CHECKING:
    from .edge import BaseSurrealEdge

logger = logging.getLogger(__name__)

_E = TypeVar("_E", bound="BaseSurrealEdge")
_V = TypeVar("_V")

# Per-class derivations of v0.18.0, computed once on first use. They cannot be built in
# ``__init_subclass__``: that runs inside ``type.__new__``, before Pydantic has finished
# assembling ``model_fields``, so the aliases would not be visible yet. Keyed on the class
# object, which is what makes them inheritance-correct — a subclass gets its own entry.
_ALIAS_MAPS: dict[type, tuple[dict[str, str], dict[str, str]]] = {}
_SERVER_FIELDS: dict[type, frozenset[str]] = {}
# The read kinds of v0.23.0 (``BaseSurrealModel._read_kinds``), filled once Pydantic completed the class.
_READ_KINDS: dict[type, dict[str, str]] = {}
# The fields a write sync validates (#228): their annotations (``BaseSurrealModel._sync_fields``) and
# the adapters built from them on first use, ``None`` for one that cannot be built (``_sync_adapter``).
_SYNC_FIELDS: dict[type, dict[str, Any]] = {}
_SYNC_ADAPTERS: dict[type, dict[str, TypeAdapter[Any] | None]] = {}
# The fields the model's own validators may reshape (``BaseSurrealModel._sync_reshaped``), where a
# field validated alone would differ from a read (#228).
_SYNC_RESHAPED: dict[type, frozenset[str]] = {}
# ``_sync_reshaped()`` of a model whose own validators may reshape every field.
_ALL_FIELDS: frozenset[str] = frozenset({"*"})
# The key under which a node of ``_with_sdk_timedeltas``'s trie holds the value that replaces it (#228).
_LEAF = object()
# Whether validating a model may run code of the user's on its input (``BaseSurrealModel._runs_input_code``, #228).
_INPUT_CODE: dict[type, bool] = {}
# Whether validating a model may refuse the SDK's ``Duration`` where ``set_data`` does not convert it
# (``BaseSurrealModel._may_refuse_sdk_durations``, #228).
_MAY_REFUSE_DURATIONS: dict[type, bool] = {}
# The core schema types of a validator function, which may receive the input itself (``_schema_runs_input_code``).
_INPUT_FUNCTIONS = frozenset({"function-before", "function-after", "function-wrap", "function-plain"})

# ``tuple`` and ``Annotated`` subscripted with annotations known at run time (mypy takes them for types).
_TUPLE: Any = tuple
_ANNOTATED: Any = typing.Annotated

# Pydantic's metaclass sits between ``__pydantic_init_subclass__`` and the user's ``class``
# statement; a warning raised there must skip its frames to land on the user's line (v0.22.0).
_PYDANTIC_DIR = os.path.dirname(os.path.abspath(pydantic.__file__))


def _legacy_table_name(cls: type) -> str | None:
    """Return the deprecated ``_table_name: ClassVar[str]`` of ``cls`` (or a base), else ``None``.

    Only the ``ClassVar`` form is a plain ``str`` on the class, and it is the only form that ever
    named a table. ``_table_name = "t"`` and ``_table_name: str = "t"`` are Pydantic **private
    attributes** — the class answers a ``ModelPrivateAttr`` — and are ignored, as they always
    should have been (the ``QuerySet`` used to interpolate that object's ``repr`` into SurrealQL).
    """
    legacy = getattr(cls, "_table_name", None)
    return legacy if isinstance(legacy, str) else None


def _check_table_name(cls: type, key: str, name: Any) -> None:
    """Refuse a table name that is not a SurrealQL identifier: it is interpolated into queries.

    Raises:
        TypeError: ``name`` is not a ``str``.
        ValueError: ``name`` is not an identifier (letters, digits, underscores; not starting
            with a digit). ``fullmatch`` so a trailing newline does not slip through ``$``.
    """
    if not isinstance(name, str):
        raise TypeError(f"{cls.__name__}: {key} must be a str, got {type(name).__name__}.")
    if not VALID_ALIAS_PATTERN.fullmatch(name):
        raise ValueError(
            f"{cls.__name__}: {key} {name!r} must be an identifier (letters, digits, underscores; "
            "not starting with a digit): it is interpolated into SurrealQL."
        )


def _read_kind(annotation: Any) -> str | None:
    """Return the read kind of a field annotation: ``"timedelta"``, ``"duration"``, ``"geometry"`` or ``None``.

    A geometry field (v0.24.0) — ``GeoField[...]``, an alias such as ``PointField``, an ORM
    geometry class or a union of them, with or without ``None`` — is ``"geometry"``. It is checked
    first, so ``GeoField["point|polygon"]`` has a kind too; ``list[GeometryPoint]``,
    ``GeometryPoint | str`` and the SDK's classes have none.

    Otherwise (v0.23.0) unwraps ``Annotated``, then a union of exactly one non-``None`` member with
    ``None``, then ``Annotated`` again. Only exactly ``timedelta`` or exactly the ORM's ``Duration``
    has a kind: ``timedelta | str``, ``list[timedelta]``, a ``timedelta`` subclass or
    ``Range[timedelta]`` has none.
    """
    if _geo._geo_kinds(annotation) is not None:
        return "geometry"
    if typing.get_origin(annotation) is typing.Annotated:
        annotation = typing.get_args(annotation)[0]
    if typing.get_origin(annotation) in (typing.Union, UnionType):
        members = typing.get_args(annotation)
        if len(members) != 2 or type(None) not in members:
            return None
        annotation = members[0] if members[1] is type(None) else members[1]
        if typing.get_origin(annotation) is typing.Annotated:
            annotation = typing.get_args(annotation)[0]
    if annotation is timedelta:
        return "timedelta"
    if annotation is Duration:
        return "duration"
    return None


def _is_input(node: Any, value: Any, copies: Mapping[int, Any] | None) -> bool:
    """Whether ``node``, found in the data, is ``value``, the input of an error, or the SDK ``Duration`` whose copy it is (#228).

    ``copies`` is the ``memo`` of the deep copy a first run validated instead of the data
    (:func:`_validated_with_sdk_timedeltas`), which maps the ``id`` of each copied object to its copy.
    """
    if node is value:
        return True
    return copies is not None and type(node) is SdkDuration and copies.get(id(node)) is value


def _path_to(
    data: Any, loc: Sequence[int | str], value: Any, copies: Mapping[int, Any] | None = None
) -> list[int | str] | None:
    """The keys and indexes that lead from ``data`` to ``value`` along an error's ``loc``, or ``None`` (#228).

    ``loc`` names ``dict`` keys and ``list`` or ``tuple`` indexes, mixed with the tags Pydantic
    adds for a union member (``"RfSub"``, ``"timedelta"``…), which name nothing in the data: a
    ``str`` that is not a key of the current ``dict``, or that meets a ``list`` or ``tuple``, is
    skipped, and so is the rest of ``loc`` once ``value`` itself is reached. Only plain ``dict``,
    ``list`` and ``tuple`` containers are entered. A path is returned only when it ends on
    ``value`` itself (identity), so a tag that happens to be a key never leads to another value.
    When the error comes from a deep copy of ``data`` (``copies``, see :func:`_is_input`), the
    path must end on the SDK ``Duration`` of ``data`` whose copy ``value`` is.
    """
    path: list[int | str] = []
    node = data
    for part in loc:
        if _is_input(node, value, copies):
            break
        container = type(node)
        key = container is dict and part in node
        index = container in (list, tuple) and type(part) is int and 0 <= part < len(node)
        if key or index:
            node = node[part]
        elif type(part) is str and container in (dict, list, tuple):
            continue
        else:
            return None
        path.append(part)
    return path if _is_input(node, value, copies) else None


def _copy_with(node: Any, items: Mapping[int | str, Any]) -> Any:
    """A copy of the container ``node`` with ``items`` set in it, of the same type: ``dict``, ``list`` or ``tuple`` (#228).

    The only place :func:`_rebuilt` copies a container, once per container on a converted path;
    ``node`` itself is never mutated, and its other items are kept by identity.
    """
    if type(node) is dict:
        return {**node, **items}
    copied = list(node)
    for index, value in items.items():
        copied[typing.cast(int, index)] = value
    return copied if type(node) is list else tuple(copied)


def _rebuilt(node: Any, trie: Mapping[Any, Any]) -> Any:
    """``node`` with the values the ``trie`` of :func:`_with_sdk_timedeltas` names replaced, in one pass (#228).

    A ``trie`` node maps a key or index of ``node`` to the ``trie`` of the item found there, or
    holds the new value under ``_LEAF`` when ``node`` itself is replaced. The paths come from
    :func:`_path_to`, so they enter only plain ``dict``, ``list`` and ``tuple`` containers: each
    one on a path is copied once (:func:`_copy_with`), and every item off the paths is kept by
    identity.
    """
    if _LEAF in trie:
        return trie[_LEAF]
    return _copy_with(node, {part: _rebuilt(node[part], child) for part, child in trie.items()})


def _with_sdk_timedeltas(data: Any, error: ValidationError, copies: Mapping[int, Any] | None = None) -> Any:
    """``data`` with the SDK's ``Duration`` values a ``timedelta`` refused converted, or ``None`` when there is none (#228).

    Pydantic's ``timedelta`` refuses the SDK's ``Duration`` (``time_delta_type``), the value every
    read returns for a SurrealDB ``duration``. For each such error whose input is exactly the SDK's
    class, the input found in ``data`` at the error's ``loc`` (:func:`_path_to`) is replaced by
    ``_timedelta_from_sdk(input)``, floored to the microsecond. The paths are gathered first, then
    ``data`` is rebuilt in one pass (:func:`_rebuilt`): each container on a path is copied once,
    whatever the number of values below it, and ``data`` itself is never mutated. Two errors that
    lead to one position (a union of models with the same field) convert it once. An input that
    rule leaves alone (negative, beyond ``timedelta.max``) and an input the ``loc`` does not lead
    to (a ``mode="before"`` validator rebuilt the data…) are not converted. ``copies``: the error
    comes from a deep copy of ``data`` (see :func:`_path_to`). Never raises.
    """
    try:
        trie: dict[Any, Any] = {}
        for detail in error.errors(include_url=False, include_context=False):
            value = detail.get("input")
            if detail["type"] != "time_delta_type" or type(value) is not SdkDuration:
                continue
            converted = _timedelta_from_sdk(value)
            path = None if converted is value else _path_to(data, detail["loc"], value, copies)
            if path is None:
                continue
            node = trie
            for part in path:
                node = node.setdefault(part, {})
            node.setdefault(_LEAF, converted)
        return _rebuilt(data, trie) if trie else None
    except Exception:
        return None


def _validated_with_sdk_timedeltas(validate: Callable[[Any], _V], data: Any, isolate: bool = False) -> _V:
    """``validate(data)``, run once more with the SDK's durations converted when a ``timedelta`` refused them (#228).

    A ``timedelta`` below the top level of a model (a sub-model's field, ``list[timedelta]``,
    ``timedelta | str``…) then reads the SDK's ``Duration`` as a top-level one does
    (:meth:`BaseSurrealModel.set_data`), floored to the microsecond. Without such a value
    (:func:`_with_sdk_timedeltas`), the error of ``validate(data)`` is raised; otherwise the second
    run gives the result, so an error then names what is really wrong with the data. ``data``
    itself is never mutated by the conversion.

    The second run never starts from input the first one may have changed. ``isolate`` says that
    ``validate`` may run code of the user's on its input (:func:`_schema_runs_input_code`), which
    may change ``data`` in place: a ``mode="before"`` validator that reads stored cents as a price,
    or an ``after`` validator or ``model_post_init`` that appends to a list Pydantic passed through
    (``Any``), would then be applied twice. The first run then validates a deep copy of ``data``,
    and the second starts again from ``data``, untouched, converting there only the SDK
    ``Duration`` whose copy the first run refused (:func:`_path_to`), so that code is applied once
    to the result. A value that code moved is not found, and stays refused. Nothing is copied when
    ``data`` holds no SDK ``Duration`` (:func:`_holds_sdk_duration`), since there is nothing to
    convert; when ``data`` cannot be copied, there is no second run, and the error of the first is
    raised, as without the conversion. The accepted residual: on a model with such a ``timedelta``
    and with code of the user's, a first run that succeeds builds the instance from the copy, so
    objects the caller passed are held as copies when the input also holds the SDK's own
    ``Duration`` (reads are unaffected, since the row is the SDK's).
    """
    copies: dict[int, Any] | None = None
    first = data
    if isolate:
        if not _holds_sdk_duration(data):
            return validate(data)
        copies = {}
        try:
            first = copy.deepcopy(data, copies)
        except Exception:
            return validate(data)
    try:
        return validate(first)
    except ValidationError as exc:
        fixed = _with_sdk_timedeltas(data, exc, copies)
        if fixed is None:
            raise
    return validate(fixed)


def _holds_sdk_duration(data: Any) -> bool:
    """Whether ``data`` is the SDK's ``Duration`` or holds one in its plain ``dict``, ``list`` and ``tuple`` containers (#228).

    At any depth: the places :func:`_with_sdk_timedeltas` can convert one (:func:`_path_to`). The
    walk does not recurse and enters each container once, so a deep or self-referencing value
    cannot exhaust it.
    """
    stack: list[Any] = [data]
    seen: set[int] = set()
    while stack:
        node = stack.pop()
        kind = type(node)
        if kind is SdkDuration:
            return True
        if (kind is dict or kind is list or kind is tuple) and id(node) not in seen:
            seen.add(id(node))
            stack.extend(node.values() if kind is dict else node)
    return False


def _schema_runs_input_code(schema: Any) -> bool:
    """Whether a Pydantic core schema may run code of the user's on its input, at any depth (#228).

    Every hook that runs code of the user's during validation counts, because a value Pydantic
    passes through (``Any``, ``object``, an arbitrary class) is the input object itself, so even
    code that runs after its field validated receives it. That is a validator function of any mode
    (``before``, ``after``, ``wrap``, ``plain``: a model or field validator, a ``BeforeValidator``
    or ``AfterValidator`` in ``Annotated``, a custom type's validator…), a model with its own
    ``__init__`` or ``model_post_init``, a dataclass with a ``__post_init__``, a callable
    ``Discriminator`` and a ``default_factory`` that takes the validated data (see
    :func:`_node_runs_input_code`). Such code may change a ``dict`` or ``list`` of the input in
    place, which a second validation of that input would apply twice (see
    :func:`_validated_with_sdk_timedeltas`). Every node of the schema is walked, nested models and
    definitions included, except serializers (a ``serialization`` schema), which never see the
    input. The ORM's and Pydantic's own functions do not count (:func:`_is_library_callable`):
    Pydantic's ``model_post_init`` of a model with private attributes, for one. Anything
    unexpected, a schema that is not a ``dict`` included, gives ``True``, the safe answer. Never
    raises.
    """
    if not isinstance(schema, dict):
        return True
    try:
        stack: list[Any] = [schema]
        seen: set[int] = set()
        while stack:
            node = stack.pop()
            if not isinstance(node, dict | list | tuple) or id(node) in seen:
                continue
            seen.add(id(node))
            if not isinstance(node, dict):
                stack.extend(node)
                continue
            if _node_runs_input_code(node):
                return True
            stack.extend(value for key, value in node.items() if key != "serialization")
        return False
    except Exception:
        return True


def _node_runs_input_code(node: dict[str, Any]) -> bool:
    """Whether one node of a core schema runs code of the user's during validation (see :func:`_schema_runs_input_code`).

    A validator function (``function-before``, ``-after``, ``-wrap`` or ``-plain``) that is not
    library code; a ``model`` with its own ``__init__`` (``custom_init``) or whose ``post_init``
    hook is not library code; a ``dataclass`` whose ``__post_init__`` is not library code; a
    ``tagged-union`` whose discriminator is a callable that is not library code; a ``default`` whose
    ``default_factory`` takes the validated data and is not library code. May raise on an
    unexpected node, which the caller counts as ``True``.
    """
    kind = node.get("type")
    if kind in _INPUT_FUNCTIONS:
        function = node.get("function")
        return not isinstance(function, dict) or not _is_library_callable(function.get("function"))
    if kind == "model":
        post_init = node.get("post_init")
        return bool(node.get("custom_init")) or (
            post_init is not None and not _is_library_callable(getattr(node["cls"], post_init))
        )
    if kind == "dataclass":
        return bool(node.get("post_init")) and not _is_library_callable(node["cls"].__post_init__)
    if kind == "tagged-union":
        discriminator = node.get("discriminator")
        return callable(discriminator) and not _is_library_callable(discriminator)
    if kind == "default":
        return bool(node.get("default_factory_takes_data")) and not _is_library_callable(node.get("default_factory"))
    return False


def _schema_refuses_sdk_durations(schema: Any, cls: type, top_level: Collection[str]) -> bool:
    """Whether validating ``cls`` may refuse the SDK's ``Duration`` where ``set_data`` does not convert it (#228).

    ``schema`` is the core schema of ``cls`` and ``top_level`` the names of its fields that
    :meth:`BaseSurrealModel.set_data` converts (read kind ``timedelta``). Pydantic refuses the SDK's
    ``Duration`` at a ``timedelta`` node, so the answer is whether the schema holds one anywhere
    except as the direct schema of such a field of ``cls`` (in its own ``model-fields``), reached
    from the field only through ``default`` and ``nullable`` wrappers: ``ttl: timedelta | None`` does
    not count, ``timedelta | str``, ``list[timedelta]``, ``Annotated[timedelta, AfterValidator(…)]``
    and a ``timedelta`` in a sub-model, a dataclass or a ``TypedDict`` do. The ORM's ``Duration``
    and ``Range`` hold no ``timedelta`` node. Every node is walked, nested models and definitions
    included, except serializers (a ``serialization`` schema), which never validate. Anything
    unexpected, a schema that is not a ``dict`` included, gives ``True``, the safe answer. Never
    raises.
    """
    if not isinstance(schema, dict):
        return True
    try:
        allowed: set[int] = set()
        stack: list[Any] = [schema]
        seen: set[int] = set()
        while stack:
            node = stack.pop()
            if not isinstance(node, dict | list | tuple):
                continue
            if isinstance(node, dict) and node.get("type") == "timedelta":
                if id(node) not in allowed:
                    return True
                allowed.discard(id(node))  # one field holds it: met again elsewhere, it counts
                continue
            if id(node) in seen:
                continue
            seen.add(id(node))
            if not isinstance(node, dict):
                stack.extend(node)
                continue
            if node.get("type") == "model" and node.get("cls") is cls:
                allowed |= _top_level_timedelta_nodes(node, top_level)
            stack.extend(value for key, value in node.items() if key != "serialization")
        return False
    except Exception:
        return True


def _top_level_timedelta_nodes(model: Mapping[str, Any], top_level: Collection[str]) -> set[int]:
    """The ``id`` of each ``timedelta`` node that is the direct schema of a field in ``top_level`` (#228).

    ``model`` is the ``model`` node of the class. Its ``model-fields`` sit below the class's own
    ``mode="before"`` validators (``set_data`` among them) and v1 root validators; each field's
    schema is followed through ``default`` and ``nullable`` wrappers only. A field whose schema
    leads anywhere else gives nothing, so its ``timedelta`` node counts
    (:func:`_schema_refuses_sdk_durations`). May raise on an unexpected node, which the caller
    counts as ``True``.
    """
    fields = model["schema"]
    while fields.get("type") in ("function-before", "function-after"):
        fields = fields["schema"]
    if fields.get("type") != "model-fields":
        return set()
    found: set[int] = set()
    for name, field in fields["fields"].items():
        if name not in top_level:
            continue
        node = field["schema"]
        while node.get("type") in ("default", "nullable"):
            node = node["schema"]
        if node.get("type") == "timedelta":
            found.add(id(node))
    return found


def _is_library_callable(func: Any) -> bool:
    """Whether a validator or ``model_post_init`` comes from the ORM, the SDK or Pydantic rather than the user's code.

    The SDK's own validator is its ``RecordID``'s. Read from the module of the function (a
    ``classmethod`` validator is bound: its ``__func__``;
    a ``functools.partial``: the function it wraps). Pydantic's wrapper of a user
    ``model_post_init`` copies the user's module (``functools.wraps``), so it counts as the user's.
    A callable without a module counts as the user's too. Used by
    :meth:`BaseSurrealModel._sync_reshaped` and :func:`_schema_runs_input_code` (#228).
    """
    while isinstance(func, functools.partial):
        func = func.func
    module = getattr(getattr(func, "__func__", func), "__module__", None)
    return isinstance(module, str) and module.partition(".")[0] in ("surreal_orm_lite", "surrealdb", "pydantic")


def _own_annotation_names(cls: type) -> set[str]:
    """Return the names annotated in **this** class's body, ignoring inherited ones.

    Python 3.14 (PEP 649) evaluates annotations lazily, so they are no longer stored in
    ``cls.__dict__["__annotations__"]`` by the time ``__init_subclass__`` runs — only an
    ``__annotate_func__`` is. Reading the old location there silently yields ``{}``, which
    would quietly disable any logic built on it. ``annotationlib`` is the supported accessor;
    its ``STRING`` format returns the annotations unevaluated, so a forward reference can't
    raise here.
    """
    try:
        import annotationlib  # type: ignore[import-not-found]
    except ImportError:  # Python < 3.14
        return set(cls.__dict__.get("__annotations__", {}))
    return set(annotationlib.get_annotations(cls, format=annotationlib.Format.STRING))


def _snapshot_moment(instance: BaseModel) -> datetime | None:
    """The moment a time-travel read (``QuerySet.as_of()``) returned *instance* at; ``None`` for a current record (v0.26.0).

    The only reader of the private marker ``_surreal_as_of``: never read ``instance._surreal_as_of`` itself. An
    instance pickled by v0.25.x, whose models had no private attribute, may carry ``__pydantic_private__ = None``
    (``BaseSurrealModel.__setstate__`` repairs it when it is unpickled, but not one built otherwise), and
    Pydantic's ``__getattr__`` then raises ``TypeError`` on the attribute instead of answering.
    """
    return (instance.__pydantic_private__ or {}).get("_surreal_as_of")


class SurrealConfigDict(ConfigDict, total=False):
    """
    SurrealConfigDict is a configuration dictionary for SurrealDB models.

    Every key is optional, for type checkers too (``total=False``, v0.22.0): set only the ones
    the model needs, e.g. ``SurrealConfigDict(table_name="purchased")``.

    Attributes:
        table_name (str | None): Override the table name; default: the class name. Full
            SurrealDB-ORM key (v0.22.0). Must be an identifier — checked when the class is
            defined — and inherited by subclasses unless they set their own.
        primary_key (str | None): The primary key field name for the model.
        access_name (str | None): Name of the DEFINE ACCESS method (v0.17.0).
        identifier_field (str | None): Field a user signs in with (v0.17.0).
        password_field (str | None): Field holding the password hash (v0.17.0).
        auth_algorithm (str | None): crypto:: hash family used by the access method.
        auth_duration_token (str | None): JWT lifetime, e.g. "15m".
        auth_duration_session (str | None): Session lifetime, e.g. "12h".
        auth_duration_grant (str | None): Refresh-grant lifetime; 3.x, WITH REFRESH only.
        with_refresh (bool | None): Emit WITH REFRESH — SurrealDB 3.x only.
        server_fields (list[str] | None): Columns the server owns (v0.18.0). They are dropped
            from implicit write payloads and hydrated back from the row the write returns,
            but stay writable when a caller names one explicitly.
        connection (str | None): The named connection this model reads and writes (v0.29.0,
            ``SurrealDBConnectionManager.add_connection()``); default: ``"default"``. Full
            SurrealDB-ORM key. Checked when the class is defined (a non-empty ``str``), inherited
            by subclasses unless they set their own; registered or not is checked at the model's
            first database call.
        events (Sequence[Event] | None): Server-side events of the model's table (v0.31.0) —
            ``Event(...)`` objects, rendered by ``event_ddl()`` and applied by ``define_events()``.
            Documented as ``Event`` in the full SurrealDB-ORM; here a ``SurrealConfigDict`` key, as
            every model option. Checked when the class is defined (a ``list`` or ``tuple`` of
            ``Event`` with distinct names), inherited by subclasses unless they set their own — a
            subclass with another table defines the same events on its own table.
    """

    table_name: str | None
    " Override the table name; default: the class name. Full SurrealDB-ORM key. "

    connection: str | None
    ' The named connection this model reads and writes (v0.29.0); default: ``"default"``. Full SurrealDB-ORM key. '

    events: Sequence[Event] | None
    " Server-side events of the model's table (v0.31.0): Event(...) objects, applied by define_events(). "

    primary_key: str | None
    " The primary key field name for the model. "

    # --- v0.18.0: columns the server owns (see BaseSurrealModel.get_server_fields) ---
    server_fields: list[str] | None

    # --- v0.17.0: model-level authentication (see model_auth.AuthenticatedUserMixin) ---
    access_name: str | None
    identifier_field: str | None
    password_field: str | None
    auth_algorithm: str | None
    auth_duration_token: str | None
    auth_duration_session: str | None
    auth_duration_grant: str | None
    with_refresh: bool | None


class BaseSurrealModel(BaseModel):
    """
    Base class for models interacting with SurrealDB.
    """

    model_config = ConfigDict(populate_by_name=True)
    """Accept either name for an aliased field (v0.18.0).

    ``Field(alias="password_hash")`` renames the **column**, not the Python attribute, so both
    spellings have to validate: the Python name in application code, the alias when hydrating a
    row the server sent. Pydantic's default is alias-only, which would make ``User(password=…)``
    a validation error in the very code the alias exists to keep readable.
    """

    __surreal_computed__: typing.ClassVar[dict[str, str]] = {}
    """Computed field name → SurrealQL expression, collected by ``__init_subclass__``."""

    __surreal_edge__: typing.ClassVar[bool] = False
    """``True`` on :class:`~surreal_orm_lite.edge.BaseSurrealEdge` and its subclasses (v0.22.0).

    The internal marker the write paths, ``relate()`` and live hydration test to tell an edge
    model from a node, so they need not import ``edge.py`` (which imports this module).
    """

    _surreal_as_of: datetime | None = PrivateAttr(default=None)
    """The moment a time-travel read (``QuerySet.as_of()``) returned this instance at; ``None`` for a current record (v0.26.0).

    Set by the QuerySet's time-travel reads only, on the instances they return and on the record models nested
    in their fields, such as a link ``fetch()`` resolves into a model-typed field (``QuerySet._mark_snapshots()``).
    Read it through :func:`_snapshot_moment`, never as an attribute: an instance pickled by v0.25.x has no
    private state. While it is set, the instance is a read-only snapshot: every write (``save()``, ``update()``,
    ``merge()``, ``patch()``, ``upsert()``, ``delete()``, the ``atomic_*()`` methods, ``relate()``,
    ``remove_relation()``, ``remove_all_relations()``, and ``QuerySet.bulk_create()``) raises ``SurrealDbError``
    before any signal or request; ``refresh()`` clears it. A private attribute, so it never reaches
    ``model_dump()`` or a write; ``model_copy()`` and ``pickle`` keep it, and Pydantic's ``==`` compares it.
    """

    def __setstate__(self, state: dict[Any, Any]) -> None:
        """Unpickle; an instance pickled before v0.26.0 has no snapshot marker: it is a current record (v0.26.0).

        v0.25.x models had no private attribute, so their pickled state holds ``__pydantic_private__: None``
        (or, on a model declaring its own, a dict without the marker). The marker is added as ``None``: such an
        instance equals a fresh equal one and writes as before the upgrade. A snapshot pickled by v0.26.0 keeps
        its moment, and a model's own private attributes are kept.
        """
        super().__setstate__(state)
        private = self.__pydantic_private__
        if private is None:
            # Pydantic's own __setstate__ sets this slot the same way (its __setattr__ refuses dunder names).
            object.__setattr__(self, "__pydantic_private__", {"_surreal_as_of": None})
        else:
            private.setdefault("_surreal_as_of", None)

    def _reject_snapshot_write(self, method: str) -> None:
        """Refuse *method* (named with its ``()``) on a snapshot, an instance read with ``as_of()`` (v0.26.0).

        The first statement of every instance write, so nothing runs before it: no signal, no transaction, no
        request. The marker is read through :func:`_snapshot_moment`, the only reader (an instance pickled by
        v0.25.x may have no private state).

        Raises:
            SurrealDbError: the instance holds the record as it was at a moment, not the current record.
        """
        moment = _snapshot_moment(self)
        if moment is not None:
            raise SurrealDbError(_time_travel.snapshot_write_refused(type(self).__name__, method, moment))

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Collect ``Computed(...)`` defaults before Pydantic builds the model.

        Runs inside ``type.__new__``, i.e. before Pydantic's ``ModelMetaclass`` finishes
        collecting fields — so replacing each sentinel with ``None`` here is what turns the
        attribute into an ordinary nullable field with a ``None`` default.

        Inherited expressions are merged first (bases in reverse MRO), then **this** class body
        decides every name it re-annotates: redeclaring a field with a new ``Computed(...)``
        overrides the parent's expression, and redeclaring it as an ordinary field demotes it
        back to a normal writable column. Without that second rule the field would keep being
        stripped from write payloads and rejected by the write guards, silently discarding the
        subclass's data.
        """
        collected: dict[str, str] = {}
        for base in reversed(cls.__mro__[1:]):
            # Same rule as for this class body below, applied to every base: a base that
            # re-annotates a name re-decides it. Merging the bases' verdicts alone would let
            # an *ordinary* redeclaration in a sibling base be ignored — with
            # ``class C(B, A)`` where ``B.x: str`` and ``A.x: Computed[str]``, Pydantic builds
            # a writable ``x`` while the verdict still called it computed, so ``x`` was
            # stripped from writes and ``merge(x=…)`` raised on a field the user owns.
            for name in _own_annotation_names(base):
                collected.pop(name, None)
            collected.update(getattr(base, "__surreal_computed__", None) or {})
        # A name this class re-annotates is re-decided below; drop the inherited verdict first.
        for name in _own_annotation_names(cls):
            collected.pop(name, None)
        for name, value in list(vars(cls).items()):
            if isinstance(value, _ComputedDefault):
                collected[name] = value.expression
                setattr(cls, name, None)
        cls.__surreal_computed__ = collected
        super().__init_subclass__(**kwargs)

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        """Check the table name once Pydantic has finished building the model (v0.22.0).

        ``__init_subclass__`` runs too early: the merged ``model_config`` does not exist yet.
        A bad name fails at the ``class`` statement rather than on the first query, because
        ``get_table_name()`` is interpolated into SurrealQL. The ``connection`` key (v0.29.0) is
        checked here too: a non-empty ``str``, or ``None`` for the default connection. So is the
        ``events`` key (v0.31.0): ``None``, or a ``list`` or ``tuple`` of ``Event`` with distinct
        names — an inherited value is checked again, the messages naming the subclass.

        The deprecated ``_table_name: ClassVar[str]`` is checked the same way and warns once,
        where it is defined — a subclass inheriting it does not warn again.
        """
        super().__pydantic_init_subclass__(**kwargs)
        configured = cls.model_config.get("table_name")
        if configured is not None:
            _check_table_name(cls, "table_name", configured)
        connection = cls.model_config.get("connection")
        if connection is not None:
            check_config_connection(cls, connection)
        events = cls.model_config.get("events")
        if events is not None:
            check_events_config(cls, events)
        legacy = cls.__dict__.get("_table_name")
        if isinstance(legacy, str):
            _check_table_name(cls, "_table_name", legacy)
            message = (
                f"{cls.__name__}._table_name is deprecated and will be removed in a future minor; "
                "use model_config = SurrealConfigDict(table_name=…)."
            )
            if isinstance(configured, str):
                message += " table_name wins."
            warnings.warn(message, DeprecationWarning, stacklevel=user_stacklevel(also_skip=(_PYDANTIC_DIR,)))

    @classmethod
    def get_computed_fields(cls) -> dict[str, str]:
        """Return ``{field_name: SurrealQL expression}`` for this model's computed fields.

        Declaration order, inherited fields first — except a field a subclass *redeclares*,
        which moves to the end (it is re-decided by the subclass body). Order only affects the
        sequence of generated DDL statements, never evaluation: SurrealDB evaluates computed
        fields alphabetically.

        Returns a copy — this is the single source of truth every write path consults. ``{}``
        for a model with no computed fields.
        """
        return dict(cls.__surreal_computed__)

    @classmethod
    def computed_field_ddl(cls, overwrite: bool = True) -> list[str]:
        """Render the ``DEFINE FIELD … VALUE`` statements for this model's computed fields.

        Pure: builds the SurrealQL and returns it without touching the database, so it can be
        printed, diffed, or fed into a migration. One statement per computed field, in
        declaration order; ``[]`` when the model has none.

        Args:
            overwrite: ``True`` (default) emits ``DEFINE FIELD OVERWRITE …`` — the model is the
                source of truth, so re-running converges the database onto the code and an
                edited expression takes effect. ``False`` emits ``DEFINE FIELD IF NOT EXISTS …``,
                which never disturbs an existing definition.

        Note:
            No ``TYPE`` clause is emitted: SurrealDB infers an optional type from the
            expression on both 2.6.x and 3.x. Explicit field types are a later roadmap item.
        """
        computed = cls.get_computed_fields()
        if not computed:
            return []
        table = cls.get_table_name()
        validate_alias_name(table)
        clause = "OVERWRITE" if overwrite else "IF NOT EXISTS"
        statements: list[str] = []
        for name, expression in computed.items():
            column = cls.to_db_field(name)
            validate_alias_name(column)
            statements.append(f"DEFINE FIELD {clause} {column} ON {table} VALUE {expression};")
        return statements

    @classmethod
    @_cache.invalidates("cls")
    async def define_computed_fields(
        cls,
        overwrite: bool = True,
        tx: Transaction | None = None,
    ) -> list[str]:
        """Apply this model's computed-field definitions to the database.

        Runs the statements from :meth:`computed_field_ddl` and returns them. Safe to call at
        application start-up: with the default ``overwrite=True`` it is idempotent and
        converges the database onto the model. A model with no computed fields is a no-op
        returning ``[]``.

        Statements are executed one per call so a failure can name the offending field. The
        two server lines raise different SDK exceptions for a bad expression
        (``InternalError`` on 2.6.x, ``ValidationError`` on 3.x — both ``ServerError``), so
        both are normalised to :class:`SurrealDbError`.

        Args:
            overwrite: see :meth:`computed_field_ddl`.
            tx: optional transaction the DDL is run in. On a buffered transaction (HTTP or
                SurrealDB 2.6.x) the statements are deferred to commit, so an invalid
                expression surfaces there rather than here.

        Example:
            >>> await Player.define_computed_fields()
            ["DEFINE FIELD OVERWRITE full_name ON Player VALUE string::concat(...);"]
        """
        statements = cls.computed_field_ddl(overwrite=overwrite)
        if not statements:
            return []
        # Cheap even inside a transaction: get_client() returns the already-connected client.
        client = await cls._connection_manager().get_client()
        return await apply_ddl_statements(statements, client=client, tx=tx, what="computed field")

    # ==================== Server-side events (v0.31.0) ====================

    @classmethod
    def get_events(cls) -> tuple[Event, ...]:
        """Return the events declared in ``model_config = SurrealConfigDict(events=[...])``, in declaration order.

        A tuple; ``()`` when the model declares none. A subclass inherits its parent's events unless it sets
        ``events`` itself (``events=[]`` drops them).
        """
        events = cls.model_config.get("events")
        # Checked at the class statement: a list or tuple of Event, or None.
        return tuple(events) if isinstance(events, (list, tuple)) else ()

    @classmethod
    def event_ddl(cls, *, overwrite: bool = True) -> list[str]:
        """Render the ``DEFINE EVENT`` statements of this model's events on its table (v0.31.0).

        Pure: builds the SurrealQL without touching the database (no connection needed), the same text
        every time, so it can be printed, diffed or fed into a migration. One statement per event of
        :meth:`get_events`, in declaration order, each ``Event.ddl()`` on :meth:`get_table_name`; ``[]`` when the
        model declares none.

        Args:
            overwrite: ``True`` (default) emits ``DEFINE EVENT OVERWRITE …`` — the model is the source of truth,
                so re-running converges the database onto the code. ``False`` emits
                ``DEFINE EVENT IF NOT EXISTS …``, which never disturbs an existing definition.

        Example:
            >>> Account.event_ddl()
            ["DEFINE EVENT OVERWRITE `email_audit` ON `account` WHEN $before.email != $after.email THEN { … };"]
        """
        table = cls.get_table_name()
        return [event.ddl(table, overwrite=overwrite) for event in cls.get_events()]

    @classmethod
    async def define_events(cls, *, overwrite: bool = True, tx: Transaction | None = None) -> list[str]:
        """Apply this model's events to its table (v0.31.0) and return the statements applied.

        Runs the statements of :meth:`event_ddl` on the model's connection (the ``using()`` block's, else
        ``SurrealConfigDict(connection=…)``, else ``"default"``), one statement per call, in declaration order.
        Safe to call at application start-up: with the default ``overwrite=True`` it is idempotent and converges
        the database onto the model. A model without events is a no-op returning ``[]``, with no request. An event
        the model no longer declares is left in place: remove it with :meth:`remove_event`.

        The events' expressions are written into the DDL as given: they are trusted application code, never user
        input (``Event`` checks them for mistakes only). Applying needs a session allowed to define (a system
        user such as root, or a namespace or database ``OWNER``/``EDITOR``).

        Without *tx*, a refusal names the event and its table, never its statement, whose ``when`` and ``then``
        may hold a secret: ``Can't apply event definition: 'email_audit' on table 'account' -> <the server's
        reason>``. With *tx*, an error raised when the transaction ends can still carry the server's message,
        statement excerpt included (#278).

        Args:
            overwrite: see :meth:`event_ddl`.
            tx: optional transaction the statements are applied with. Without one, the first refusal stops the
                rest and the events before it stay defined.

        Raises:
            SurrealDbError: an event has ``async_mode=True`` and the connection runs SurrealDB 2.x or the
                embedded engine — before any DDL; *tx* belongs to another connection — before any request; the
                server refused a statement (``Can't apply event definition: …``). In a buffered transaction (*tx*
                over HTTP, on SurrealDB 2.x or the embedded engine) the statements are only queued: a refusal
                comes back when the block commits, as the transaction's own error, never as
                ``SurrealDbAuthenticationError``.
            SurrealDbAuthenticationError: the session may not define events.

        Example:
            >>> await Account.define_events()
            ["DEFINE EVENT OVERWRITE `email_audit` ON `account` WHEN $before.email != $after.email THEN { … };"]
        """
        check_transaction(cls, tx, f"{cls.__name__}.define_events()")
        statements = cls.event_ddl(overwrite=overwrite)
        if not statements:
            return []
        manager = cls._connection_manager()
        # Cheap even inside a transaction: get_client() returns the already-connected client.
        client = await manager.get_client()
        await ensure_async_events_supported(client, cls.get_events(), embedded=manager.is_embedded())
        subjects = [event_subject(event.name, cls.get_table_name()) for event in cls.get_events()]
        return await apply_ddl_statements(statements, client=client, tx=tx, what="event", classify=True, subjects=subjects)

    @classmethod
    async def remove_event(cls, event: str, *, missing_ok: bool = True, tx: Transaction | None = None) -> str:
        """Remove the event named *event* from this model's table (v0.31.0) and return the statement applied.

        Removal is by name: the event need not be declared in ``SurrealConfigDict(events=…)`` — removing an event
        the model no longer declares is the point. *event* is an identifier (letters, digits, underscores; not
        starting with a digit), checked before any request.

        Args:
            event: the event's name.
            missing_ok: ``True`` (default) emits ``REMOVE EVENT IF EXISTS …``, so a missing event is not an error;
                ``False`` raises ``SurrealDbNotFoundError`` for it.
            tx: optional transaction the statement is applied with.

        Raises:
            TypeError, ValueError: *event* is not an identifier, or *missing_ok* not a ``bool``.
            SurrealDbNotFoundError: ``missing_ok=False`` and the table has no such event.
            SurrealDbAuthenticationError: the session may not remove events.
            SurrealDbError: *tx* belongs to another connection — before any request; any other refusal. In a
                buffered transaction (*tx* over HTTP, on SurrealDB 2.x or the embedded engine) the statement is only
                queued: a refusal comes back when the block commits, as the transaction's own
                ``SurrealDbError``, never as ``SurrealDbNotFoundError`` or ``SurrealDbAuthenticationError`` —
                ``missing_ok=False`` included.

        Example:
            >>> await Account.remove_event("email_audit")
            'REMOVE EVENT IF EXISTS `email_audit` ON `account`;'
        """
        check_transaction(cls, tx, f"{cls.__name__}.remove_event()")
        table = cls.get_table_name()
        statement = remove_event_statement(event, table, missing_ok)
        client = await cls._connection_manager().get_client()
        await apply_ddl_statements(
            [statement],
            client=client,
            tx=tx,
            what="event",
            kind="removal",
            classify=True,
            subjects=[event_subject(event, table)],
        )
        return statement

    # ==================== Field aliases & server-owned fields (v0.18.0) ====================

    @classmethod
    def _implicit_aliases(cls) -> dict[str, str]:
        """Return the column renames the model class itself implies, ``{python_name: column}``.

        Empty on a node. An edge model maps its ``in_`` field to the ``in`` column (v0.22.0):
        ``in`` is a Python keyword, so the field cannot carry that name, and a
        ``Field(alias="in")`` would make every ``Edge(in_=…)`` call a type-checker error. The
        entries go through :meth:`_alias_maps` exactly like a ``Field(alias=…)``.
        """
        return {}

    @classmethod
    def _alias_maps(cls) -> tuple[dict[str, str], dict[str, str]]:
        """Return this model's ``(python → column, column → python)`` alias maps, cached.

        Built from :meth:`_implicit_aliases` (``in_ → in`` on an edge model, v0.22.0) and then
        ``FieldInfo.alias``. A separate ``validation_alias`` / ``serialization_alias`` — and in
        particular an ``AliasPath``/``AliasChoices`` — does not describe a column *rename*,
        which is the whole contract here, so those are deliberately left to Pydantic and never
        enter this map. Both sources pass the same ambiguity check, so a user field aliased to
        an implied column (``in``) is refused like any other collision.

        Never overridden: the implied entries are read through the hook, so the per-class cache
        holds the complete map whichever class method builds it first.
        """
        cached = _ALIAS_MAPS.get(cls)
        if cached is None:
            forward = {
                **cls._implicit_aliases(),
                **{name: info.alias for name, info in cls.model_fields.items() if info.alias},
            }
            cls._reject_ambiguous_aliases(forward)
            cached = (forward, {column: name for name, column in forward.items()})
            _ALIAS_MAPS[cls] = cached
        return cached

    @classmethod
    def _read_kinds(cls) -> dict[str, str]:
        """Return the read kind of each top-level ``timedelta``/``Duration``/geometry field, cached per class.

        ``{"ttl": "timedelta", "grace": "duration", "location": "geometry"}``, keyed by the Python
        field name **and** by its column when the field is aliased: :meth:`set_data` receives rows
        keyed by column and calls keyed by name. The columns are those of :meth:`_alias_maps`, read
        without its ambiguity check so that a read never raises here. Only a field annotated exactly
        ``timedelta`` or ``Duration`` (through ``Annotated`` and ``X | None``, v0.23.0) or a geometry
        field (v0.24.0, see ``_read_kind``) has a kind; a model without one gets ``{}``, and the read
        rules are then no-ops. Cached once Pydantic has completed the class, like the alias maps;
        never mutate the returned dict.
        """
        cached = _READ_KINDS.get(cls)
        if cached is not None:
            return cached
        implicit = cls._implicit_aliases()
        kinds: dict[str, str] = {}
        for name, info in cls.model_fields.items():
            kind = _read_kind(info.annotation)
            if kind is None:
                continue
            kinds[name] = kind
            column = info.alias or implicit.get(name)
            if column:
                kinds[column] = kind
        if cls.__pydantic_complete__:
            _READ_KINDS[cls] = kinds
        return kinds

    @classmethod
    def _sync_fields(cls) -> dict[str, Any]:
        """Return the fields a write sync validates, by Python name, cached per class (#228, #261).

        ``{"status": Status, "sub": Sub | None, "location": Annotated[GeometryPoint | None, GeoField['point']]…}``:
        every declared field but ``id``, which keeps the native ``RecordID`` a row comes back with, and the top-level
        ``timedelta`` and ``Duration`` fields, which :meth:`_apply_record` converts by their read kind
        (:meth:`_read_kinds`). Before #261 only a field holding a sub-model, a geometry or a ``timedelta`` below the
        top level was validated, and every other one kept the SDK's value even where a read gives another type: an
        enum's ``str`` or ``int``, a ``list`` for a ``tuple`` or a set, a midnight ``datetime`` for a ``date``, the
        SDK's ``Range``, a ``Decimal`` for a ``float`` stored as ``decimal``. Each maps to its full annotation,
        ``Annotated`` constraints included (``FieldInfo.rebuild_annotation()``), plus the field's discriminator.
        :meth:`_apply_record` receives rows keyed by column but translates each key to its Python name first, so the
        map needs no column keys. Cached once Pydantic has completed the class, like :meth:`_read_kinds`; never mutate
        the returned dict.
        """
        cached = _SYNC_FIELDS.get(cls)
        if cached is not None:
            return cached
        fields: dict[str, Any] = {}
        for name, info in cls.model_fields.items():
            if name == "id" or _read_kind(info.annotation) in ("timedelta", "duration"):
                continue
            annotation = info.rebuild_annotation()
            if info.discriminator is not None:
                annotation = _ANNOTATED[annotation, pydantic.Field(discriminator=info.discriminator)]
            fields[name] = annotation
        if cls.__pydantic_complete__:
            _SYNC_FIELDS[cls] = fields
        return fields

    @classmethod
    def _sync_adapter(cls, name: str) -> TypeAdapter[Any] | None:
        """Return the adapter that validates field ``name`` on a write sync, built on first use and cached per class (#228).

        ``TypeAdapter(tuple[<annotation>], config=<the model's config>)``: the 1-tuple puts the
        annotation where a field's annotation sits in the model, so a stdlib dataclass or a
        ``TypedDict`` inherits the model's config as it does in a read, and the config is accepted
        whatever the annotation (Pydantic refuses one for a bare ``BaseModel``). ``None`` when the
        field is not in :meth:`_sync_fields` or the adapter cannot be built; an annotation that
        cannot be resolved may also give an adapter that fails on use, which
        :meth:`_validated_on_sync` treats like a value that does not validate. Never raises.
        """
        adapters = _SYNC_ADAPTERS.get(cls)
        if adapters is None:
            adapters = {}
            if cls.__pydantic_complete__:
                _SYNC_ADAPTERS[cls] = adapters
        if name in adapters:
            return adapters[name]
        annotation = cls._sync_fields().get(name)
        adapter: TypeAdapter[Any] | None = None
        if annotation is not None:
            try:
                adapter = TypeAdapter(_TUPLE[annotation], config=typing.cast(ConfigDict, cls.model_config))
            except Exception:
                adapter = None
        adapters[name] = adapter
        return adapter

    @classmethod
    def _sync_reshaped(cls) -> frozenset[str]:
        """Return the fields the model's own validators may reshape, cached per class (#228).

        A field validated alone (:meth:`_validated_on_sync`) misses the validators and hooks of the
        model that holds it, which a read runs: a ``field_validator`` (or a v1 ``validator``) aimed
        at the field, and a ``model_validator`` (any mode), a v1 ``root_validator`` or a
        ``model_post_init``, which may reshape any field. The result names the fields of the first
        kind, or is ``_ALL_FIELDS`` when one of the second kind, or a ``field_validator("*")``,
        exists. The ORM's and Pydantic's own (``set_data``, ``check_config``, the ``in_``/``out``
        validators of an edge…) do not count (``_is_library_callable``). Read from
        ``__pydantic_decorators__``; anything unexpected there gives ``_ALL_FIELDS``, the safe
        answer. Cached once Pydantic has completed the class, like :meth:`_sync_fields`.
        """
        cached = _SYNC_RESHAPED.get(cls)
        if cached is not None:
            return cached
        reshaped = _ALL_FIELDS
        try:
            decorators = cls.__pydantic_decorators__
            whole: list[Any] = [*decorators.model_validators.values(), *decorators.root_validators.values()]
            per_field: list[Any] = [*decorators.field_validators.values(), *decorators.validators.values()]
            if all(_is_library_callable(d.func) for d in whole) and _is_library_callable(cls.model_post_init):
                names = {field for d in per_field if not _is_library_callable(d.func) for field in d.info.fields}
                reshaped = _ALL_FIELDS if "*" in names else frozenset(names)
        except Exception:
            reshaped = _ALL_FIELDS
        if cls.__pydantic_complete__:
            _SYNC_RESHAPED[cls] = reshaped
        return reshaped

    @classmethod
    def _runs_input_code(cls) -> bool:
        """Whether validating this model may run code of the user's on its input, cached per class (#228).

        :func:`_schema_runs_input_code` of the model's core schema, which holds the validators and
        hooks of its sub-models and of its fields' types too. Every hook that runs code of the user's
        counts, whatever its mode: a pass-through field (``Any``…) hands the input object itself to
        an ``after`` validator or a ``model_post_init``. Such code may change the input in place, so
        the conversion of the SDK's durations below the top level validates a copy first
        (:meth:`_read_nested_timedeltas`). ``True`` while Pydantic has not completed the class (its
        schema is not built yet), and cached only once it has, like :meth:`_sync_fields`.
        """
        cached = _INPUT_CODE.get(cls)
        if cached is not None:
            return cached
        if not cls.__pydantic_complete__:
            return True
        runs = _schema_runs_input_code(cls.__pydantic_core_schema__)
        _INPUT_CODE[cls] = runs
        return runs

    @classmethod
    def _may_refuse_sdk_durations(cls) -> bool:
        """Whether validating this model may refuse an SDK ``Duration`` that :meth:`set_data` does not convert, cached per class (#228).

        :func:`_schema_refuses_sdk_durations` of the model's core schema: ``True`` when it holds a
        ``timedelta`` anywhere but as a top-level field ``set_data`` converts, for instance in a
        sub-model, a dataclass, a ``TypedDict``, ``list[timedelta]`` or ``timedelta | str``. Only
        such a model takes the retry of :meth:`_read_nested_timedeltas`; every other one validates
        as before #228, without a copy. ``True`` while Pydantic has not completed the class, and
        cached only once it has, like :meth:`_runs_input_code`.
        """
        cached = _MAY_REFUSE_DURATIONS.get(cls)
        if cached is not None:
            return cached
        if not cls.__pydantic_complete__:
            return True
        kinds = cls._read_kinds()
        top_level = frozenset(name for name in cls.model_fields if kinds.get(name) == "timedelta")
        refuses = _schema_refuses_sdk_durations(cls.__pydantic_core_schema__, cls, top_level)
        _MAY_REFUSE_DURATIONS[cls] = refuses
        return refuses

    @classmethod
    def _validated_row(cls, record: Mapping[str, Any]) -> Self | None:
        """Return the row validated as a read validates it, or ``None`` when it does not validate (#228).

        ``model_validate`` of the row, as :meth:`from_db` does: the model's own validators run,
        ``set_data`` and ``model_post_init`` included. It validates a deep copy, since ``set_data``
        rewrites the ``id`` in place and a ``mode="before"`` validator may rewrite nested dicts in
        place: the row stays as received. Any exception (a value that does not validate, a
        validator that raises anything else, a model Pydantic cannot complete) gives ``None``.
        """
        try:
            return cls.model_validate(copy.deepcopy(dict(record)))
        except Exception:
            return None

    @classmethod
    def _validated_on_sync(cls, name: str, value: Any) -> Any:
        """Return ``value`` validated against field ``name`` alone, else ``value`` itself (#228).

        The fallback of :meth:`_apply_record` when the whole row does not validate
        (:meth:`_validated_row`). A sub-model's ``dict`` becomes the declared instance, with the
        ORM's durations, ranges and geometries; a stored GeoJSON object in a geometry field becomes
        the ORM's geometry; a ``timedelta`` below the top level reads the SDK's ``Duration``, as in
        a read (``_validated_with_sdk_timedeltas``); and since #261 an enum's value becomes the
        enum, a ``list`` the declared ``tuple`` or set, a midnight ``datetime`` the declared
        ``date``, the SDK's ``Range`` the ORM's and a ``Decimal`` the declared ``float``. That is
        what a read gives unless the model's own validators reshape the field, which a field
        validated alone cannot run: a field in :meth:`_sync_reshaped` gives ``value`` back
        unchanged. So do a value that does not validate, a validator that raises anything else
        and a field without an adapter (see :meth:`_sync_adapter`): never raises. When the model may run code of the user's on its
        input (:meth:`_runs_input_code`), a deep copy of ``value`` is validated, so ``value`` is
        given back as received, never as that code changed it in place.
        """
        reshaped = cls._sync_reshaped()
        if reshaped is _ALL_FIELDS or name in reshaped:
            return value
        adapter = cls._sync_adapter(name)
        if adapter is None:
            return value
        isolate = cls._runs_input_code()
        try:
            received = (copy.deepcopy(value),) if isolate else (value,)
            return _validated_with_sdk_timedeltas(adapter.validate_python, received, isolate)[0]
        except Exception:
            return value

    @classmethod
    def _reject_ambiguous_aliases(cls, forward: Mapping[str, str]) -> None:
        """Raise if the alias map is not a bijection between field names and columns.

        Two shapes make the translation lossy, and both are silent without this check:

        - a column that is also **another field's** name (``pw = Field(alias="hash")`` next to
          a field called ``hash``) — the payload's two keys collapse into one, so one field is
          dropped, and the reverse map sends the column to the wrong attribute;
        - two fields sharing **one column** — same collapse, no survivor worth picking.

        A field aliased to its own name is harmless and allowed.
        """
        stolen = sorted(
            f"{name} -> {column}" for name, column in forward.items() if column != name and column in cls.model_fields
        )
        if stolen:
            raise ValueError(
                f"{cls.__name__}: alias {', '.join(stolen)} collides with another field of that name. "
                "A column name that is also a field name makes writes and hydration ambiguous; "
                "rename the field or choose a different alias."
            )
        seen: dict[str, str] = {}
        for name, column in forward.items():
            if column in seen:
                raise ValueError(
                    f"{cls.__name__}: {seen[column]} and {name} both alias the column {column!r}. "
                    "Each field needs its own column."
                )
            seen[column] = name

    @staticmethod
    def _translate_field(name: str, mapping: Mapping[str, str]) -> str:
        """Map ``name`` through ``mapping``, translating only its first dotted segment.

        A nested path belongs to its root field: if ``password`` is stored as ``password_hash``
        then ``password.inner`` is stored as ``password_hash.inner``. This is the same rule the
        computed-field and atomic-op guards apply to nested paths. An unmapped name passes
        through unchanged, so every call site can translate unconditionally.
        """
        if not mapping or not name:
            return name
        head, separator, rest = name.partition(".")
        mapped = mapping.get(head)
        return name if mapped is None else mapped + separator + rest

    @classmethod
    def get_field_aliases(cls) -> dict[str, str]:
        """Return ``{python_field_name: surrealdb_column}`` for this model's aliased fields.

        ``{}`` on a node when no field declares ``Field(alias=…)``; an edge model always holds
        ``{"in_": "in"}`` (v0.22.0). Returns a copy: the underlying map is cached per class and
        every write path reads it.
        """
        return dict(cls._alias_maps()[0])

    @classmethod
    def to_db_field(cls, name: str) -> str:
        """Translate a Python field name to the SurrealDB column it is stored under."""
        return cls._translate_field(name, cls._alias_maps()[0])

    @classmethod
    def to_py_field(cls, column: str) -> str:
        """Translate a SurrealDB column back to the Python attribute that holds it."""
        return cls._translate_field(column, cls._alias_maps()[1])

    @classmethod
    def _columns_for(cls, data: Mapping[str, Any] | None) -> dict[str, Any]:
        """Re-key a Python-named mapping to the columns it writes to.

        The boundary helper for every wire payload assembled from keyword arguments
        (``merge(**data)``, ``server_values=``). Values are untouched; a model with no aliases
        gets an ordinary copy back.
        """
        if not data:
            return {}
        return {cls.to_db_field(key): value for key, value in data.items()}

    @classmethod
    def _py_fields(cls, payload: Mapping[str, Any]) -> list[str]:
        """Name a column-keyed payload's fields the way a signal handler expects them.

        ``update_fields`` reaches user code, which knows the model's Python attributes and not
        the columns behind them — ``if "display" in update_fields`` has to keep working on an
        aliased model, and it is what ``merge()`` has always sent.
        """
        return [cls.to_py_field(key) for key in payload]

    @classmethod
    def get_server_fields(cls) -> frozenset[str]:
        """Return the columns the **server** owns — never volunteered in a write payload.

        The union of two sources, because they are the same thing from the ORM's side:

        - ``model_config = SurrealConfigDict(server_fields=[…])`` — columns a ``DEFINE FIELD …
          DEFAULT``/``VALUE``, an event or a trigger fills in, which the ORM does not define
          itself;
        - this model's computed fields, which are server-owned by construction.

        They differ on one point only, and it is about *explicit* writes rather than payload
        building: naming a computed field in ``merge()``/``bulk_update()`` raises (the server
        would discard it — an invisible no-op), while naming a ``server_fields`` entry is
        allowed, because a caller who spells the column out is deliberately overriding it.

        Raises:
            ValueError: if ``server_fields`` names a field the model does not declare — a typo
                there would otherwise silently protect nothing.
        """
        cached = _SERVER_FIELDS.get(cls)
        if cached is None:
            configured = cls.model_config.get("server_fields") or ()
            if isinstance(configured, str) or not isinstance(configured, Iterable):
                raise TypeError(f"{cls.__name__}: server_fields must be a list of field names, got {configured!r}.")
            names = tuple(configured)
            undeclared = sorted(name for name in names if name not in cls.model_fields)
            if undeclared:
                raise ValueError(
                    f"{cls.__name__}: server_fields names {', '.join(undeclared)}, "
                    f"which {'is' if len(undeclared) == 1 else 'are'} not declared on the model. "
                    "Add the field, or drop the name from server_fields."
                )
            cached = frozenset(names) | frozenset(cls.get_computed_fields())
            _SERVER_FIELDS[cls] = cached
        return cached

    @classmethod
    def get_table_name(cls) -> str:
        """Return the table this model reads and writes — the one name every query path uses.

        ``model_config = SurrealConfigDict(table_name=…)`` when set (v0.22.0); else the
        deprecated ``_table_name: ClassVar[str]``; else the class name.
        """
        configured = cls.model_config.get("table_name")
        if isinstance(configured, str):
            return configured
        legacy = _legacy_table_name(cls)
        return legacy if legacy is not None else cls.__name__

    @classmethod
    def get_connection_name(cls) -> str:
        """The connection this model's database calls use right now (v0.29.0) — the full SurrealDB-ORM's method.

        The ``using()`` override when a block is active, else ``model_config = SurrealConfigDict(connection=…)``,
        else ``"default"``. Resolved at every database call (a QuerySet built outside a ``using()`` block and run
        inside it uses the block's connection); a live stream or a change feed resolves it once, when it starts.
        Registration is checked at the first database call, not here.
        """
        active = SurrealDBConnectionManager.get_active_connection_name()
        if active is not None:
            return active
        configured = cls.model_config.get("connection")
        return configured if isinstance(configured, str) else "default"

    @classmethod
    def _connection_manager(cls) -> type[SurrealDBConnectionManager]:
        """The connection class of :meth:`get_connection_name` (v0.29.0): the one way a model reaches its connection.

        :raises ValueError: the connection is not registered — at the model's first database call, never at
            class definition nor in a queryset builder.
        """
        name = cls.get_connection_name()
        try:
            return SurrealDBConnectionManager._manager_for(name)
        except ValueError:
            raise ValueError(
                f"{cls.__name__}: connection {name!r} is not registered. Register it with "
                f"SurrealDBConnectionManager.add_connection({name!r}, …) first."
            ) from None

    @classmethod
    def _endpoint_columns(cls) -> frozenset[str]:
        """Return the columns of this model that hold an edge endpoint (v0.22.0).

        ``in``/``out`` on an edge model, none on a node. The QuerySet hands them to the filter
        builder, which reads a ``"table:id"`` string given for one of them as the record id it
        names (:func:`~surreal_orm_lite.utils.coerce_endpoint`) and refuses text lookups there.
        """
        return frozenset()

    @classmethod
    def _write_protected_fields(cls) -> frozenset[str]:
        """Return the fields of this model no write may change once the record exists (v0.22.0).

        Python names. ``in_``/``out`` on an edge model — SurrealDB ignores a change to an edge's
        endpoints on both lines — none on a node.
        """
        return frozenset()

    @classmethod
    def get_index_primary_key(cls) -> str | None:
        """
        Get the primary key field name for the model.
        """
        if hasattr(cls, "model_config"):  # pragma: no cover
            primary_key = cls.model_config.get("primary_key", None)
            if isinstance(primary_key, str):
                return primary_key

        return None

    def get_id(self) -> None | str | RecordID:
        """
        Get the raw ID of the model instance.

        Returns the value as stored: a native ``RecordID`` for records loaded
        from the database, or the raw value for in-memory instances.
        """
        if hasattr(self, "id"):
            id_value = self.id  # type: ignore[attr-defined]
            return id_value if id_value is not None else None

        if hasattr(self, "model_config"):
            primary_key = self.model_config.get("primary_key", None)
            if isinstance(primary_key, str) and hasattr(self, primary_key):
                primary_key_value = getattr(self, primary_key)
                return primary_key_value if primary_key_value is not None else None

        return None  # pragma: no cover

    def get_raw_id(self) -> str | None:
        """Return the bare identifier portion (without the table prefix)."""
        id_value = self.get_id()
        if id_value is None:
            return None
        if isinstance(id_value, RecordID):
            return str(id_value.id)
        return str(id_value)

    def _record_id(self) -> RecordID | None:
        """Return a ``RecordID`` suitable for passing to SDK 2.0 methods."""
        id_value = self.get_id()
        if id_value is None:
            return None
        if isinstance(id_value, RecordID):
            return id_value
        return RecordID(self.get_table_name(), id_value)

    @classmethod
    def from_db(cls, record: dict | list) -> Self | list[Self]:
        """
        Create an instance from a SurrealDB record.
        """
        if isinstance(record, list):
            return [cls.from_db(rs) for rs in record]  # type: ignore

        return cls(**record)

    @model_validator(mode="before")
    @classmethod
    def set_data(cls, data: Any) -> Any:
        """
        Pre-process data before model validation.
        Extracts the ID from RecordID if present, unless the field accepts RecordID natively.

        A top-level ``timedelta`` field given the SDK's ``Duration`` (what every read returns for a
        SurrealDB ``duration``) gets a ``timedelta``, floored to the microsecond (v0.23.0). The dict is
        copied on that first change, never mutated by it; a value this rule cannot convert (beyond
        ``timedelta.max``, a subclass, any other type) is left as received for Pydantic to judge,
        so the rule itself never raises. A ``timedelta`` below the top level reads the SDK's
        ``Duration`` through :meth:`_read_nested_timedeltas` (#228).
        """
        if isinstance(data, dict) and "id" in data and isinstance(data["id"], RecordID):
            # Only convert to plain string when the id field does not accept RecordID.
            id_field = cls.model_fields.get("id")
            if id_field is not None:
                annotation = id_field.annotation
                args = typing.get_args(annotation)
                field_accepts_record_id = RecordID in (args if args else (annotation,))
            else:
                field_accepts_record_id = False
            if not field_accepts_record_id:
                # Use the RecordID's raw identifier directly. Avoid str(rid).split(":")
                # which yields the SurrealQL-escaped form (e.g. "⟨1⟩") for numeric-looking
                # or special string ids.
                data["id"] = str(data["id"].id)
        kinds = cls._read_kinds()
        if kinds and isinstance(data, dict):
            received = data
            for key, kind in kinds.items():
                if kind != "timedelta" or key not in data:
                    continue
                value = data[key]
                converted = _timedelta_from_sdk(value)
                if converted is not value:
                    if data is received:
                        data = dict(data)
                    data[key] = converted
        return data

    @model_validator(mode="wrap")
    @classmethod
    def _read_nested_timedeltas(cls, data: Any, handler: ModelWrapValidatorHandler[Self]) -> Self:
        """Read the SDK's ``Duration`` in a ``timedelta`` below the top level, as :meth:`set_data` does at the top (#228).

        A sub-model's ``timedelta`` field, ``list[timedelta]``, ``dict[str, timedelta]``,
        ``timedelta | str``…: Pydantic refuses the SDK's ``Duration`` there, which every read
        returns for a SurrealDB ``duration``. When the validation fails on such values, it runs
        once more with each of them converted where Pydantic refused it, floored to the
        microsecond (see ``_validated_with_sdk_timedeltas``), on a copy: the conversion never
        mutates the caller's data. A value the top-level rule would leave alone stays refused, and
        a model without such a value fails as before. A ``wrap`` validator runs outside every
        ``mode="before"`` one, the model's own included, so they run again on the second try, as do
        the hooks inside the model: its sub-models' validators of any mode, ``model_post_init``,
        ``__post_init__``… When the model may run code of the user's on its input
        (:meth:`_runs_input_code`; every hook counts, since a pass-through field such as ``Any``
        hands the input object itself to ``after`` code), the first try validates a deep copy of
        the data and the second starts again from the data as received, so code that changes its
        input in place is applied once to the result. A model whose validation cannot refuse such a
        value (:meth:`_may_refuse_sdk_durations`: no ``timedelta`` but top-level ones ``set_data``
        converts) validates as before #228: ``handler(data)``, nothing checked, copied or retried.
        """
        if not cls._may_refuse_sdk_durations():
            return handler(data)
        return _validated_with_sdk_timedeltas(handler, data, cls._runs_input_code())

    async def refresh(self, tx: Transaction | None = None) -> None:
        """Refresh the model instance from the database.

        When ``tx`` is provided, the read participates in the transaction. This requires an
        ``InteractiveTransaction`` (WebSocket + SurrealDB 3.x); on a ``BufferedTransaction``
        (HTTP or SurrealDB 2.6.x) reads are not supported and this raises ``SurrealDbError``.

        Turns a snapshot (an instance read with ``QuerySet.as_of()``, whose writes are refused) back into
        the current record (v0.26.0). When the record has been deleted since, the existing "no record found"
        ``SurrealDbError`` is raised and the instance stays a snapshot. On a model mixing
        ``AuthenticatedUserMixin``, whose classmethod ``refresh(refresh_token)`` takes this name, call
        ``BaseSurrealModel.refresh(instance)``.

        A ``tx`` of another connection than the model's raises ``SurrealDbError`` before anything (v0.29.0).
        """
        if tx is not None:
            check_transaction(type(self), tx, f"{type(self).__name__}.refresh()")
            if not tx.is_interactive:
                raise SurrealDbError(
                    "refresh() is a read; it requires a WebSocket connection to SurrealDB 3.x "
                    "(native interactive transactions)."
                )
            if not self.get_id():
                raise SurrealDbError("Can't refresh data, not recorded yet.")
            rows = await tx.run_read("SELECT * FROM $rid;", {"rid": self._record_id()})
            record = rows[0] if isinstance(rows, list) and rows else rows
            if not isinstance(record, dict):
                raise SurrealDbError("Can't refresh data, no record found.")
            self._apply_record(record)
            if _snapshot_moment(self) is not None:
                self._surreal_as_of = None
            return
        if not self.get_id():
            raise SurrealDbError("Can't refresh data, not recorded yet.")  # pragma: no cover

        client = await type(self)._connection_manager().get_client()
        record = await client.select(self._record_id())

        if record is None:
            raise SurrealDbError("Can't refresh data, no record found.")  # pragma: no cover

        # SDK 2.0 returns a list even for single record select
        if isinstance(record, list):
            if len(record) == 0:
                raise SurrealDbError("Can't refresh data, no record found.")  # pragma: no cover
            record = record[0]

        # Update current instance with refreshed data (keep native RecordID for id)
        self._apply_record(record)
        if _snapshot_moment(self) is not None:
            self._surreal_as_of = None

    def _write_payload(self, replace: bool = False, keep: Collection[str] = ()) -> dict[str, Any]:
        """Return this instance's data for a write: no ``id``, no client-owned server column.

        Keys come out as **column** names. The re-keying goes through :meth:`_columns_for`
        rather than ``model_dump(by_alias=True)`` on purpose: ``by_alias`` would also honour a
        ``serialization_alias``, which :meth:`_alias_maps` deliberately ignores because it does
        not describe a symmetric column rename. Letting the two disagree would write the row
        under a name that ``filter()``, ``select()`` and ``_apply_record()`` never look for —
        the value would land in the database and become unreachable through the ORM.

        What gets excluded depends on what omitting a field *means* in the statement being
        built, which is not the same for a create and a replace:

        - **Computed fields** are excluded always. ``DEFINE FIELD … VALUE`` re-evaluates on
          every write, create and replace alike, so sending one is at best wasted bytes and at
          worst — if the definition has not been applied yet — a ``None`` that nulls the column.
        - **``server_fields``** are excluded from a **create** only. There, omitting the column
          is precisely how the server's ``DEFAULT`` gets to apply. In a **replace**
          (``UPDATE``/``UPSERT … CONTENT``) omission does not mean "server, fill this in": a
          ``DEFAULT`` is a create-time default on both 2.6.x and 3.x, so an omitted optional
          column is *deleted* and an omitted required one is a hard server error. The instance's
          value is therefore kept, which round-trips the server's own value for any record that
          was loaded or saved through the ORM: ``save()`` reads the whole returned row back
          (#261), and a ``datetime`` the instance holds floored to the microsecond keeps the stored
          nanoseconds, within the bounds of :func:`~surreal_orm_lite.utils.replace_content`.

        Args:
            replace: ``True`` for the full-replace paths (``update()``, ``upsert()``), which
                keeps ``server_fields`` in the payload for the reason above.
            keep: ``server_fields`` a caller named explicitly, written on a create after all.
                Only ``*_or_create`` passes it: there the name came from the lookup or from
                ``defaults``, and omitting it would create a row the same lookup cannot find.
                A computed field listed here is still excluded.
        """
        excluded = {"id", *self.get_computed_fields()}
        if not replace:
            excluded |= self.get_server_fields() - set(keep)
        return self._columns_for(self.model_dump(exclude=excluded))

    @classmethod
    def _is_write_protected(cls, name: str) -> bool:
        """Whether ``name`` addresses one of :meth:`_write_protected_fields` (v0.22.0).

        ``name`` is a Python field or its column (``in`` as well as ``in_``), possibly a dotted
        path, judged on its first segment: ``out.x`` writes inside ``out``.
        """
        protected = cls._write_protected_fields()
        if not protected:
            return False
        head = name.split(".", 1)[0]
        return head in protected or cls.to_py_field(head) in protected

    @classmethod
    def _reject_endpoint_writes(cls, fields: Iterable[str], context: str) -> None:
        """Raise if ``fields`` names an edge endpoint (``in_``/``in``/``out``) (v0.22.0).

        SurrealDB ignores a change to an existing edge's endpoints on both lines — ``UPDATE …
        MERGE``/``SET``/``PATCH`` write the other fields and silently keep ``in``/``out`` — so such
        a write is an invisible no-op, refused at the call site like a computed-field write. A
        node protects nothing.
        """
        if any(cls._is_write_protected(field) for field in fields):
            raise ValueError(
                f"{context}: `in`/`out` are the endpoints of edge `{cls.__name__}`; SurrealDB ignores a change "
                "to them (both lines), so the ORM refuses it — delete the edge and relate again."
            )

    @classmethod
    def _reject_computed_writes(cls, fields: Iterable[str], context: str) -> None:
        """Raise if ``fields`` names a computed field — or, on an edge model, an endpoint.

        A computed field is set by ``DEFINE FIELD … VALUE``, and the server discards whatever a
        client sends for it — so such a write is an invisible no-op. Raising turns it into an
        error at the call site instead. Update the fields the expression reads.

        An edge's endpoints are the same kind of no-op (v0.22.0, :meth:`_reject_endpoint_writes`),
        checked first. Every write guard routes through here — ``merge()`` and its
        ``server_values=``, ``bulk_update()``, both ``patch()`` entry points
        (:meth:`_reject_computed_patch`) and the atomic helpers (:meth:`_validate_atomic_field`).
        """
        names = list(fields)
        cls._reject_endpoint_writes(names, context)
        computed = cls.get_computed_fields()
        offenders = sorted(field for field in names if field in computed)
        if offenders:
            raise ValueError(
                f"{context}: {', '.join(offenders)} "
                f"{'is a computed field' if len(offenders) == 1 else 'are computed fields'} "
                f"on {cls.__name__}, set server-side by DEFINE FIELD … VALUE and not writable. "
                "Update the fields the expression reads instead."
            )

    @classmethod
    def _reject_undeclared_writes(cls, fields: Iterable[str]) -> None:
        """Raise if ``fields`` (Python names) names a field the model does not declare.

        A model that opts into extra fields (``model_config = ConfigDict(extra="allow")``) keeps
        them. Shared by ``*_or_create`` and the typed ``relate()``: a caller who mistypes a
        field name wants to hear about it rather than lose the value.
        """
        if cls.model_config.get("extra") == "allow":
            return
        undeclared = sorted(field for field in fields if field not in cls.model_fields)
        if undeclared:
            raise SurrealDbError(
                f"{cls.__name__} does not declare {', '.join(undeclared)}; "
                "remove the key, add the field to the model, or set "
                'model_config = ConfigDict(extra="allow") to store undeclared fields.'
            )

    @classmethod
    def _validate_atomic_field(cls, field: str) -> str:
        """Validate an atomic-op target and return the **column** it addresses.

        Dotted paths are legal here (``validate_field_name`` allows them for nested fields),
        so the guard compares the **first** segment: a computed field is server-owned in full,
        making ``tag_count.items`` no more writable than ``tag_count`` — the same rule the
        patch guard applies to ``/tag_count/0``.

        The translated name is returned rather than merely validated because the caller
        interpolates it straight into a ``SET`` clause (v0.18.0). Handing back the Python name
        would compile ``amount = array::append(amount, …)`` for a field stored as ``amt``,
        which SurrealDB happily honours — by creating a second, phantom column and leaving the
        real one untouched.
        """
        validate_field_name(field, "atomic field")
        cls._reject_computed_writes([field.split(".", 1)[0]], "atomic operation")
        return cls.to_db_field(field)

    @classmethod
    def _reject_computed_patch(cls, operations: list[dict[str, Any]], context: str) -> None:
        """Raise if a JSON Patch document writes a computed field or an edge endpoint.

        ``patch()`` is a write like any other, so it gets the same guard as ``merge()`` —
        without it, ``{"op": "replace", "path": "/full_name", …}`` would be accepted here and
        silently dropped by the server, which is the exact invisible no-op this guard exists to
        prevent.

        Only the **top-level** pointer segment is checked: a computed field is server-owned in
        full, so writing ``/full_name/0`` is no more legal than writing ``/full_name``. Both
        ``path`` and — for ``move``, which removes its source — ``from`` count as writes;
        ``copy`` only reads its ``from``.

        The empty pointer ``""`` is RFC 6901's **whole document**, so it writes every field at
        once and cannot be judged on its segment. It is judged on the keys of its ``value``
        instead — replacing the document with an object that omits the computed fields is a
        legitimate write, naming one is not.

        On an edge model the same rules refuse ``/in`` (or ``/in_``) and ``/out``, a pointer below
        them and a whole-document value naming them (v0.22.0); a ``remove`` of the whole document
        removes them too, so it is refused.

        Call **after** :func:`validate_patch_operations`, which guarantees the shape relied on
        here.
        """
        targets: list[str] = []
        for op in operations:
            pointers = [op["path"]]
            if op.get("op") == "move":
                pointers.append(op["from"])
            for pointer in pointers:
                if pointer == "":
                    value = op.get("value")
                    # No inspectable value (e.g. ``remove`` on the whole document) — treat it
                    # as touching everything the server owns, and an edge's endpoints.
                    if isinstance(value, dict):
                        targets.extend(cls._pointer_segment_field(str(key)) for key in value)
                    else:
                        targets.extend(cls.get_computed_fields())
                        targets.extend(cls._write_protected_fields())
                    continue
                targets.append(cls._pointer_segment_field(cls._pointer_head(pointer)))
        cls._reject_computed_writes(targets, context)

    @staticmethod
    def _pointer_head(pointer: str) -> str:
        """Return a JSON Pointer's first reference token, RFC 6901 escapes decoded."""
        segment = pointer.split("/")[1] if "/" in pointer else ""
        return segment.replace("~1", "/").replace("~0", "~")

    @classmethod
    def _pointer_segment_field(cls, segment: str) -> str:
        """Name the Python field a pointer's top-level segment addresses, in either vocabulary.

        ``patch()`` accepts ``/display`` and ``/display_name`` alike, so the guard has to fold
        both onto the field name ``_reject_computed_writes`` compares against. The alias map is
        a bijection (see :meth:`_reject_ambiguous_aliases`), so no segment can mean two fields.
        The maps are used directly rather than through ``to_db_field`` because a reference
        token is a literal key: a ``.`` in it is not a nested path.
        """
        forward, reverse = cls._alias_maps()
        column = forward.get(segment, segment)
        return reverse.get(column, column)

    @classmethod
    def _pointer_to_column(cls, pointer: str) -> str:
        """Rewrite a JSON Pointer's top-level segment to the column it addresses."""
        if not pointer:
            return pointer
        _, separator, rest = pointer[1:].partition("/")
        head = cls._pointer_head(pointer)
        column = cls._alias_maps()[0].get(head, head).replace("~", "~0").replace("/", "~1")
        return f"/{column}{separator}{rest}"

    @classmethod
    def _columns_for_patch(cls, operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return a copy of a JSON Patch document whose pointers address columns (v0.18.0).

        A pointer is written against the model like every other argument, so ``/display``
        has to reach the server as ``/display_name`` — sent verbatim, SurrealDB creates a
        phantom ``display`` column, leaves the real one alone, and the row it returns never
        maps back onto the attribute. A pointer already naming the column passes through
        unchanged, so callers who wrote ``/display_name`` keep working.

        ``path`` and ``from`` are rewritten; a whole-document operation (``path == ""``) has
        its object ``value`` re-keyed instead. The caller's list and dicts are not mutated.
        """
        rewritten: list[dict[str, Any]] = []
        for op in operations:
            copy = dict(op)
            copy["path"] = cls._pointer_to_column(op["path"])
            if "from" in op:
                copy["from"] = cls._pointer_to_column(op["from"])
            if op["path"] == "" and isinstance(op.get("value"), dict):
                copy["value"] = cls._columns_for(op["value"])
            rewritten.append(copy)
        return rewritten

    async def _do_save(self, tx: Transaction | None = None, keep: Collection[str] = ()) -> tuple[Self, bool]:
        """Internal save logic. Returns (self, created).

        When ``tx`` is provided the CREATE statement is buffered (deferred to commit)
        and the in-memory instance is returned as-is. Buffered creates require an
        explicit record id, which is bound (``$rid``), never written into the statement,
        so every id shape works: a compound id such as ``pair:[User:alice, User:bob]``
        included. ``keep`` is forwarded to :meth:`_write_payload`.

        The row the server returns is applied to the instance (:meth:`_apply_record`), with an
        explicit id as without one, directly as in an interactive transaction (#261): the
        instance then holds what the record holds, ``DEFAULT``-filled sub-fields, values the
        server coerced and the types a read gives included. An explicit id is kept as given
        (a ``str`` stays a ``str``); an auto id is the server's ``RecordID``. A buffered
        transaction returns no row before commit: the instance keeps its values there.
        """
        record_id = self._record_id()
        data = self._write_payload(keep=keep)
        table = self.get_table_name()

        if tx is not None:
            if record_id is not None:
                rows = await tx.add("CREATE $rid CONTENT $data;", {"rid": record_id, "data": data})
                # An interactive transaction (3.x/WebSocket) answers with the created row, applied
                # exactly as the non-tx path does. A buffered transaction defers the statement and
                # has nothing to return yet: the instance keeps its values (server-owned fields stay
                # None) until it is refreshed after commit.
                self._apply_record(rows, exclude=("id",))
                return self, True
            if not tx.is_interactive:
                raise SurrealDbError(
                    "save(tx=...) requires an explicit id on a buffered transaction "
                    "(auto-id requires a WebSocket connection to SurrealDB 3.x)."
                )
            rows = await tx.add(f"CREATE {table} CONTENT $data;", {"data": data})
            self._apply_record(rows)
            return self, True

        client = await type(self)._connection_manager().get_client()

        if record_id is not None:
            # SDK 2.0 raises a structured exception instead of returning a string.
            # The "already exists" error maps to AlreadyExistsError on SurrealDB 3.x
            # but to InternalError on 2.x (no structured kind) — both subclass
            # ServerError, so catch the base and match on the message to stay
            # faithful to the original "already exists" contract across versions.
            try:
                record = await client.create(record_id, _encoding.encode(data))
            except ServerError as e:
                if "already exists" in str(e).lower():
                    raise SurrealDbError(f"There was a problem with the database: {e}") from e
                raise
            # Apply the whole row (#261): the fields the SERVER owns (computed fields, `server_fields`),
            # the sub-fields a DEFAULT filled and the values it coerced are readable straight after
            # save(), and a following update() writes them back unchanged. _apply_record gives each
            # field the type a read gives. The id stays the one the caller gave.
            self._apply_record(record, exclude=("id",))
            return self, True

        # Auto-generate the ID
        record = await client.create(table, _encoding.encode(data))  # pragma: no cover

        if isinstance(record, list):
            raise SurrealDbError("Can't save data, multiple records returned.")  # pragma: no cover

        if record is None:
            raise SurrealDbError("Can't save data, no record returned.")  # pragma: no cover

        # Update current instance with the auto-generated ID (kept as native RecordID)
        if isinstance(record, dict):
            self._apply_record(record)
            return self, True

        raise SurrealDbError("Can't save data, no record returned.")  # pragma: no cover

    @staticmethod
    def _validate_server_values(
        server_values: Mapping[str, Any] | None,
        extra_vars: Mapping[str, Any] | None,
    ) -> None:
        """Validate the ``server_values`` / ``extra_vars`` pair before compiling a statement.

        Key syntax is checked later by :func:`build_set_clause`; what must be caught here
        is (a) a non-``SurrealFunc`` server value — once merged with the field data the
        two are indistinguishable, so it would silently be bound as a literal string
        instead of evaluated — and (b) ``extra_vars`` with nothing to reference them.
        """
        if extra_vars and not server_values:
            raise ValueError("extra_vars requires server_values: no SurrealFunc expression could reference them.")
        for key, value in (server_values or {}).items():
            if not isinstance(value, SurrealFunc):
                raise TypeError(
                    f"server_values[{key!r}] must be a SurrealFunc, got {type(value).__name__!r}. "
                    "Wrap the expression: SurrealFunc('time::now()')."
                )

    async def _do_save_server(
        self,
        tx: Transaction | None = None,
        server_values: Mapping[str, SurrealFunc] | None = None,
        extra_vars: Mapping[str, Any] | None = None,
    ) -> tuple[Self, bool]:
        """Internal save routed through ``CREATE … SET`` so server functions are evaluated.

        The native SDK ``create()`` sends data as values, so a ``SurrealFunc`` would be
        stored as its literal text. This path compiles one ``SET`` clause instead —
        functions inlined, every other value bound — which SurrealDB evaluates
        server-side. ``CONTENT $data SET …`` is not valid SurrealQL (verified on 2.6.5
        and 3.1.3), hence the full clause rather than a hybrid.
        """
        record_id = self._record_id()
        # _write_payload() is already column-keyed; server_values arrive under Python names, so
        # they are translated here — otherwise an aliased field would compile a SET against a
        # column that does not exist.
        merged: dict[str, Any] = {**self._write_payload(), **self._columns_for(server_values)}
        clause, variables = build_set_clause(merged)

        if record_id is not None:
            variables["rid"] = record_id
            statement = f"CREATE $rid SET {clause};"
        else:
            if tx is not None and not tx.is_interactive:
                raise SurrealDbError(
                    "save(tx=...) requires an explicit id on a buffered transaction "
                    "(auto-id requires a WebSocket connection to SurrealDB 3.x)."
                )
            table = self.get_table_name()
            validate_alias_name(table)
            statement = f"CREATE {table} SET {clause};"

        # `rid` is reserved on both branches (it is only *bound* on the explicit-id one), so
        # extra_vars validation behaves identically either way.
        variables = merge_extra_vars(variables, extra_vars, reserved=("rid",))

        if tx is not None:
            rows = await tx.add(statement, variables)
            self._apply_record(rows)  # interactive: applies; buffered: None → stale until commit
            return self, True

        client = await type(self)._connection_manager().get_client()
        try:
            rows = await client.query(statement, _encoding.encode(variables))
        except ServerError as e:
            # Same normalisation as the native path: "already exists" is an ORM error.
            if "already exists" in str(e).lower():
                raise SurrealDbError(f"There was a problem with the database: {e}") from e
            raise
        if not rows:
            raise SurrealDbError("Can't save data, no record returned.")
        self._apply_record(rows)
        return self, True

    @_cache.invalidates("self")
    async def _save_naming(self, fields: Iterable[str], tx: Transaction | None = None) -> Self:
        """``save()`` that also writes the ``server_fields`` among ``fields``.

        Backs the create branch of ``get_or_create``/``update_or_create``, where a server field
        can legitimately be named — as a lookup criterion or in ``defaults`` — and must then be
        stored, or the next identical lookup would miss the row and create another one.
        """
        keep = frozenset(fields) & self.get_server_fields()
        if not keep:
            return await self.save(tx=tx)
        return await self._save_with_signals(functools.partial(self._do_save, keep=keep), tx)

    @_cache.invalidates("self")
    async def save(
        self,
        tx: Transaction | None = None,
        server_values: Mapping[str, SurrealFunc] | None = None,
        extra_vars: Mapping[str, Any] | None = None,
    ) -> Self:
        """
        Save the model instance to the database.

        When ``tx`` is provided, the CREATE statement is buffered onto the
        transaction instead of being executed immediately.

        Emits pre_save and post_save signals. ``around_save`` wraps the actual write,
        so in a transaction (``tx`` provided) it is NOT emitted — the write happens at
        commit, not here — consistent with update/merge/delete in tx mode. ``post_save``
        is deferred until the transaction commits successfully; if the tx rolls back,
        ``post_save`` is NOT emitted (the write never happened).

        Args:
            tx: Optional transaction the CREATE is buffered onto.
            server_values: Field name → :class:`~surreal_orm_lite.functions.SurrealFunc`,
                evaluated **by the server** (``time::now()``, ``rand::uuid::v7()``, …)
                rather than in Python. A server value overrides a model field of the same
                name. Providing it routes the write through ``CREATE … SET``.
            extra_vars: Extra query variables the expressions reference, bound (never
                interpolated) — this is how user input reaches a function safely. Requires
                ``server_values``; a key colliding with an internal binding raises.

        Example:
            >>> await player.save(server_values={"joined_at": SurrealFunc("time::now()")})
            >>> await user.save(
            ...     server_values={"pwd": SurrealFunc("crypto::argon2::generate($password)")},
            ...     extra_vars={"password": raw_password},
            ... )

        The row the server returns is applied to the instance (#261), with an explicit id as
        without one: right after ``save()`` it holds what the record holds — ``server_fields``,
        computed fields, the sub-fields a ``DEFAULT`` filled, the values the server coerced —
        each field with the value a read gives, the model's validators included (an enum, a
        ``tuple``, a set, a ``date``, a ``float`` stored as ``decimal``…). An explicit id stays as
        given; an auto id becomes the server's ``RecordID``.

        Note:
            In a **buffered** transaction (HTTP or SurrealDB 2.6.x) the server-computed
            values are unknown until commit, so the instance keeps its pre-save values for
            those fields — ``refresh()`` after commit if you need them.
        """
        self._reject_snapshot_write("save()")
        self._validate_server_values(server_values, extra_vars)
        if server_values:
            self._reject_computed_writes(server_values, "save(server_values=)")
            do_op = functools.partial(self._do_save_server, server_values=server_values, extra_vars=extra_vars)
            return await self._save_with_signals(do_op, tx)
        return await self._save_with_signals(self._do_save, tx)

    def _apply_record(self, record: Any, only: Iterable[str] | None = None, exclude: Collection[str] = ()) -> None:
        """Copy a returned DB row's fields onto this instance (id kept as native ``RecordID``).

        Accepts the raw SDK result (a row dict, a single-element list, or ``None``) and applies
        only the fields the model declares, and its extras (:meth:`_set_synced`). Shared by every
        write path that gets a row back (``_do_save``, ``_do_upsert``, ``patch``, the atomic
        helpers, the ``server_values`` paths) so the "apply the row the server gave back" step
        lives in one place. A ``None``/empty result is a no-op — callers that must distinguish
        "no row" from "nothing to apply" check first.

        Values are written without validating the instance itself (:meth:`_set_synced`): a
        frozen field is still updated. ``only`` restricts the copy to a few known fields.

        A row arrives keyed by **column**, so each key is translated back to its Python field
        name first (v0.18.0). ``only`` and ``exclude`` are therefore matched on Python names —
        the same vocabulary the callers use, since they pass things like ``get_server_fields()``.

        Each field takes the value a read gives (#228; every field since #261, see
        :meth:`_sync_fields`). The ``id`` keeps the native ``RecordID``. Top-level ``timedelta``
        and ``Duration`` fields get the SDK's ``Duration`` converted (v0.23.0), and top-level
        geometry fields the SDK's geometries, as the ORM's geometry classes (v0.24.0). Every
        other declared field (a geometry field holding anything but an SDK geometry, a stored
        GeoJSON object…, included) takes its value from the whole row validated as
        :meth:`from_db` validates it, the model's own validators included
        (:meth:`_validated_row`): a sub-model field holds its declared instance, with the ORM's
        durations, ``timedelta``s, ranges and geometries (:meth:`_read_nested_timedeltas`); an
        enum field the enum, not the SDK's ``str`` or ``int``; a ``tuple``, set, ``date`` or
        ``Range`` field its declared type, not the SDK's ``list``, midnight ``datetime`` or
        ``Range``; a ``float`` field stored as ``decimal`` a ``float``, not a ``Decimal``. When
        the row does not validate, each field is validated alone (:meth:`_validated_on_sync`),
        unless the model's own validators may reshape it, and a value that still does not
        validate is applied as received, so this never raises. A column the model does not
        declare goes, as received, where a read puts it (:meth:`_set_synced`): to the extras under
        ``extra="allow"``, nowhere otherwise. Since the row holds what ``model_dump()`` wrote, that
        includes a Pydantic ``@computed_field``'s column, which never reaches the property (#261
        review).

        Args:
            only: restrict the copy to these field names. ``None`` (default) applies every field
                in the row.
            exclude: field names never copied (#261: ``save()`` with an explicit id keeps the id it
                was given).
        """
        if isinstance(record, list):
            record = record[0] if record else None
        if not isinstance(record, dict):
            return
        allowed = None if only is None else set(only)
        kinds = self._read_kinds()
        validated = self._sync_fields()
        values: dict[str, Any] = {}
        pending: list[str] = []
        for column, value in record.items():
            key = self.to_py_field(column)
            if (allowed is not None and key not in allowed) or key in exclude:
                continue
            kind = kinds.get(key)
            if kind == "timedelta":
                value = _timedelta_from_sdk(value)
            elif kind == "duration":
                value = _duration_from_sdk(value)
            elif kind == "geometry":
                value = _geo._geometry_from_sdk(value)
            if key in validated and not (kind == "geometry" and (value is None or isinstance(value, _geo.Geometry))):
                pending.append(key)
            values[key] = value
        if pending:
            row = self._validated_row(record)
            for key in pending:
                values[key] = getattr(row, key) if row is not None else self._validated_on_sync(key, values[key])
        self._set_synced(values)

    def _set_synced(self, values: Mapping[str, Any]) -> None:
        """Write ``values``, keyed by Python name, where a read holds them, without validating the instance (#261 review).

        A field the model declares is set with ``object.__setattr__``, so a frozen field is still updated. Any other
        key but ``id`` goes to ``__pydantic_extra__`` under ``extra="allow"``, where :meth:`from_db` puts the columns
        the model does not declare, and is dropped otherwise. So a key naming a property (a Pydantic
        ``@computed_field``, whose value ``model_dump()`` writes to the record), a method (``json``) or any other
        class attribute never becomes an attribute of the instance: it neither raises nor hides that attribute.
        """
        fields = type(self).model_fields
        extra = self.__pydantic_extra__
        for key, value in values.items():
            if key in fields:
                object.__setattr__(self, key, value)
            elif extra is not None and key != "id":
                extra[key] = value

    def _sync_after_merge(
        self,
        rows: Any,
        refresh: bool,
        data: Mapping[str, Any],
        server_values: Mapping[str, Any],
    ) -> None:
        """Bring this instance in step after a merge, from the row the server sent back.

        ``refresh=True`` applies that row and treats its absence as "no record found" — an
        ``UPDATE`` matching nothing is not a server error, it just returns nothing. Under
        ``refresh=False`` the statement asked for no row (or, in a buffered transaction, has not
        run yet), so emptiness carries no information and only the literal ``data`` can be
        applied: see :meth:`_apply_merge_data_locally`.
        """
        if not refresh:
            self._apply_merge_data_locally(data, server_values)
            return
        if not rows:
            raise SurrealDbError("Can't merge data, no record found.")
        self._apply_record(rows)

    def _apply_merge_data_locally(self, data: Mapping[str, Any], server_values: Mapping[str, Any]) -> None:
        """Apply a merge's literal kwargs to this instance without consulting the server.

        The fallback whenever no row comes back: a buffered transaction (nothing has run yet)
        or ``refresh=False`` (the row was deliberately not requested). A kwarg that
        ``server_values`` overrode is skipped — writing it would leave the instance holding a
        value that never reached the database. Keys here are Python field names, written as
        :meth:`_set_synced` writes them.
        """
        self._set_synced({key: value for key, value in data.items() if key not in server_values})

    async def _run_update(
        self,
        statement: str,
        variables: dict[str, Any],
        refresh: bool,
        data: Mapping[str, Any],
        server_values: Mapping[str, Any],
    ) -> None:
        """Run a compiled merge ``UPDATE`` outside a transaction and sync this instance.

        A missing **table** returns no rows on 2.6.x but raises ``NotFoundError`` on 3.x (the
        divergence the read paths normalise since v0.7.0); both become the same
        ``SurrealDbError`` as a missing record, so every merge path fails identically on both
        DB lines. That holds under ``refresh=False`` too: the server raises for a missing table
        whatever the statement asks back.
        """
        client = await type(self)._connection_manager().get_client()
        try:
            rows = await client.query(statement, _encoding.encode(variables))
        except NotFoundError as e:
            raise SurrealDbError("Can't merge data, no record found.") from e
        self._sync_after_merge(rows, refresh, data, server_values)

    async def _merge_native(self, record_id: Any, wire_data: dict[str, Any], data: Mapping[str, Any], refresh: bool) -> None:
        """Run a non-transactional ``merge()`` without ``server_values``.

        The SDK's ``merge()`` already answers with the merged row, which is everything a
        re-read would return — so the instance is synced from it in one round-trip, and its
        emptiness still reports a record that does not exist. ``refresh=False`` needs
        ``RETURN NONE``, which the SDK method cannot express, so that case is compiled.
        """
        if not refresh:
            statement = "UPDATE $rid MERGE $data RETURN NONE;"
            await self._run_update(statement, {"rid": record_id, "data": wire_data}, False, data, {})
            return
        client = await type(self)._connection_manager().get_client()
        try:
            row = await client.merge(record_id, _encoding.encode(wire_data))
        except NotFoundError as e:
            raise SurrealDbError("Can't merge data, no record found.") from e
        self._sync_after_merge(row, True, data, {})

    async def _save_with_signals(
        self,
        do_op: Callable[..., Awaitable[tuple[Self, bool]]],
        tx: Transaction | None,
    ) -> Self:
        """Shared ``save``/``upsert`` signal envelope around the concrete write ``do_op``.

        Emits ``pre_save`` → (``around_save`` wrap) → ``post_save``. In a transaction the write
        is buffered, so ``around_save`` is skipped and ``post_save`` is deferred to a
        successful commit — identical for both ``save()`` (``do_op=_do_save``) and ``upsert()``
        (``do_op=_do_upsert``), so the contract can't drift between them.
        """
        sender = self.__class__
        has_signals = pre_save.has_handlers(sender) or post_save.has_handlers(sender) or around_save.has_handlers(sender)

        if not has_signals:
            result, _created = await do_op(tx=tx)
            return result

        await pre_save.send(sender, instance=self)

        if tx is not None:
            result, created = await do_op(tx=tx)
            tx.enqueue_post_commit(lambda: post_save.send(sender, instance=self, created=created))
            return result

        async with around_save.wrap(sender, instance=self):
            result, created = await do_op(tx=tx)

        await post_save.send(sender, instance=self, created=created)
        return result

    async def _do_upsert(self, tx: Transaction | None = None) -> tuple[Self, bool]:
        """Internal upsert logic. Returns (self, created).

        Runs ``UPSERT $rid CONTENT $data`` through ``client.query()`` (or the transaction) —
        a full REPLACE: the record is created if absent, or entirely replaced if present
        (fields omitted from the model are dropped). An explicit id is required (there is
        nothing to match without one); it is bound (``$rid``), never written into the
        statement, so every id shape works: a compound id such as
        ``pair:[User:alice, User:bob]`` included. ``created`` is read from the statement's
        ``$before`` (``None`` → the row did not exist), so the ``post_save`` signal reports it
        truthfully — at no extra round-trip. A payload holding a ``datetime`` keeps the stored
        datetimes it did not change, as :meth:`update` does (#261, :func:`replace_content`).

        When ``tx`` is provided the ``UPSERT`` statement is buffered (deferred to commit);
        on an interactive transaction the returned row is applied to ``self``.
        """
        record_id = self._record_id()
        if record_id is None:
            raise SurrealDbError(
                "upsert() requires an explicit id (there is nothing to match without one); "
                "use save() to create a record with an auto-generated id."
            )
        data = self._write_payload(replace=True)

        # RETURN $before, $after reports both states of the row in a single statement, so
        # `created` is the truth rather than an assumption (issue #156). Verified on
        # SurrealDB 2.6.x and 3.x alike.
        content, bound = replace_content(data)
        statement = f"UPSERT $rid CONTENT {content} RETURN $before AS before, $after AS after;"
        variables = {"rid": record_id, **bound}

        if tx is not None:
            rows = await tx.add(statement, variables)
            before, after = self._split_upsert_result(rows)
            self._apply_record(after if after is not None else rows)
            return self, before is None

        client = await type(self)._connection_manager().get_client()
        rows = await client.query(statement, _encoding.encode(variables))
        before, after = self._split_upsert_result(rows)
        self._apply_record(after if after is not None else rows)
        return self, before is None

    @staticmethod
    def _split_upsert_result(rows: Any) -> tuple[Any, Any]:
        """Unwrap ``[{"before": …, "after": …}]`` into ``(before, after)``.

        A buffered transaction returns nothing at ``add()`` time, and a server that answers
        in an unexpected shape must not crash the write, so anything unrecognised degrades
        to ``(None, None)`` — the caller then reports ``created=True``, the pre-0.14.3
        behaviour.
        """
        if isinstance(rows, list) and rows and isinstance(rows[0], dict) and "after" in rows[0]:
            return rows[0].get("before"), rows[0].get("after")
        return None, None

    @_cache.invalidates("self")
    async def upsert(self, tx: Transaction | None = None) -> Self:
        """
        Insert the model instance, or fully replace it if a record with the same id exists.

        Runs ``UPSERT $rid CONTENT $data``, the record id bound as a query parameter: this is
        REPLACE semantics, so any field omitted from the model is removed from the stored
        record. For a partial update use ``merge()`` instead. An explicit id is required.
        A ``datetime`` written back unchanged keeps the stored nanoseconds, as in :meth:`update`;
        as there, SurrealDB 2.x refuses to replace an existing record when the model leaves out a
        field with a non-optional ``TYPE`` and a ``VALUE`` clause, a ``Computed`` one (#267).

        When ``tx`` is provided, the ``UPSERT`` is buffered onto the transaction.

        Emits the same signals as ``save()`` (``pre_save``/``around_save``/``post_save``);
        in a transaction ``around_save`` is skipped and ``post_save`` is deferred to a
        successful commit (consistent with ``save(tx=)``).
        """
        self._reject_snapshot_write("upsert()")
        return await self._save_with_signals(self._do_upsert, tx)

    @_cache.invalidates("self")
    async def update(self, tx: Transaction | None = None) -> Any:
        """
        Update the model instance to the database.

        A full replace (``UPDATE $rid CONTENT …``): every field the instance holds is written, and a
        field it does not hold is removed from the record. After a call that applied the returned
        row (``save()``, ``refresh()``, ``merge()``…), the instance holds the values a read gives
        (#261), and those are written: a ``float`` field stored in a ``decimal`` column writes the
        float's rounding (declare ``Decimal`` to keep every digit), and under ``extra="allow"`` the
        columns in ``model_extra`` are written back, not removed. When ``tx`` is provided, the UPDATE
        statement is buffered onto the transaction instead of being executed immediately. The
        record id is bound (``$rid``), never written into the statement, so every id shape works —
        an array id such as ``follows:[User:alice, User:bob]`` included.

        A ``datetime`` keeps the stored value when that value floors to it at the microsecond
        (#261): SurrealDB stores nanoseconds, which Python and the SDK read floored to the
        microsecond, so writing a datetime back unchanged would change the stored one and a
        ``READONLY`` field would refuse it. An instance loaded with ``get()`` or written with
        ``save()`` therefore updates a record whose ``READONLY`` datetimes are top-level fields, on
        SurrealDB 2.x, 3.x and the embedded engine, and the stored datetimes keep their
        nanoseconds; a datetime you changed is written as you set it (and still refused by a
        ``READONLY`` field). So that the statement stays bounded, only the top-level datetimes are
        sure to be kept, up to 32. Below the top level (in a sub-model, a ``list`` or a ``tuple``,
        down to three levels) a datetime is kept only while the values rebuilt to reach it fit in
        what the fields before it left of those 32, which depends on the data: a list of
        timestamped items before a sub-model is enough to use it up. A datetime not kept, and one in
        a set, is written as sent, so a ``READONLY`` field holding one may refuse ``update()``:
        change such a record with :meth:`merge`. See
        :func:`~surreal_orm_lite.utils.replace_content`. Without a datetime to keep, the statement
        is the SDK's ``update()``, as before.

        SurrealDB 2.x still refuses a full replace that leaves out a field declared with a
        non-optional ``TYPE`` and a ``VALUE`` clause, which the model does not write (a
        ``Computed`` field, such as a generated ``updated: Computed[datetime]``): there, change
        such a record with :meth:`merge` (#267). SurrealDB 3.x recomputes the field.

        Emits pre_update, post_update, and around_update signals. In tx mode,
        ``around_update`` is skipped (the write is deferred to commit) and
        ``post_update`` only fires after a successful commit.

        Returns:
            The updated row as the SDK reads it (``[]`` for a record that does not exist), or
            ``None`` in a transaction.
        """
        self._reject_snapshot_write("update()")
        sender = self.__class__
        data = self._write_payload(replace=True)
        record_id = self._record_id()
        if record_id is None:
            raise SurrealDbError("Can't update data, no id found.")
        content, bound = replace_content(data)

        has_signals = pre_update.has_handlers(sender) or post_update.has_handlers(sender) or around_update.has_handlers(sender)

        if tx is not None:
            statement = f"UPDATE $rid CONTENT {content};"
            variables = {"rid": record_id, **bound}
            if not has_signals:
                await tx.add(statement, variables)
                return None
            update_fields = self._py_fields(data)
            await pre_update.send(sender, instance=self, update_fields=update_fields)
            await tx.add(statement, variables)
            tx.enqueue_post_commit(lambda: post_update.send(sender, instance=self, update_fields=update_fields))
            return None

        client = await type(self)._connection_manager().get_client()

        if not has_signals:
            return await self._replace_direct(client, record_id, data, content, bound)

        update_fields = self._py_fields(data)
        await pre_update.send(sender, instance=self, update_fields=update_fields)

        async with around_update.wrap(sender, instance=self, update_fields=update_fields):
            result = await self._replace_direct(client, record_id, data, content, bound)

        await post_update.send(sender, instance=self, update_fields=update_fields)

        return result

    @staticmethod
    async def _replace_direct(
        client: Any, record_id: RecordID, data: dict[str, Any], content: str, bound: dict[str, Any]
    ) -> Any:
        """Run ``update()`` outside a transaction, ``content`` and ``bound`` being :func:`replace_content` of ``data``.

        A payload without a ``datetime`` to keep goes through the SDK's ``update()``, as before #261. Otherwise the compiled
        ``UPDATE $rid CONTENT …`` goes through ``client.query()``, and its single row is unwrapped as the SDK's
        ``update()`` unwraps it, so ``update()`` returns the same thing either way.
        """
        if content == PLAIN_CONTENT:
            return await client.update(record_id, _encoding.encode(data))
        rows = await client.query(f"UPDATE $rid CONTENT {content};", _encoding.encode({"rid": record_id, **bound}))
        return rows[0] if isinstance(rows, list) and len(rows) == 1 else rows

    async def _merge_server(
        self,
        tx: Transaction | None,
        server_values: Mapping[str, SurrealFunc],
        extra_vars: Mapping[str, Any] | None,
        data: dict[str, Any],
        refresh: bool = True,
    ) -> Any:
        """Partial update routed through ``UPDATE … SET`` so server functions are evaluated.

        ``UPDATE … SET`` only touches the listed fields, so this keeps ``merge``'s partial
        semantics while letting SurrealDB compute values. The statement returns the updated
        row (``RETURN AFTER`` by default), which syncs the instance — and its emptiness is how
        a missing record is detected, see :meth:`_sync_after_merge`. Only a buffered tx cannot
        tell: nothing runs before commit.

        With ``refresh=False`` the statement asks for ``RETURN NONE`` instead, so the row is
        not sent back at all. The literal kwargs are applied locally, exactly as in a buffered
        transaction. The missing-record detection goes with it: that check *is* the returned
        row.
        """
        sender = self.__class__
        record_id = self._record_id()
        if record_id is None:
            raise SurrealDbError(f"No Id for the data to merge: {data}")

        # Columns on the wire, Python names in the signal payload and the local apply below.
        merged: dict[str, Any] = {**self._columns_for(data), **self._columns_for(server_values)}
        clause, variables = build_set_clause(merged)
        variables["rid"] = record_id
        variables = merge_extra_vars(variables, extra_vars)
        statement = f"UPDATE $rid SET {clause} RETURN NONE;" if not refresh else f"UPDATE $rid SET {clause};"
        update_fields = [*data, *(key for key in server_values if key not in data)]

        has_signals = pre_update.has_handlers(sender) or post_update.has_handlers(sender) or around_update.has_handlers(sender)

        if tx is not None:
            if has_signals:
                await pre_update.send(sender, instance=self, update_fields=update_fields)
            rows = await tx.add(statement, variables)
            # A buffered tx has no row yet, so it syncs like refresh=False: literal kwargs only,
            # server-computed fields stale until a refresh() after commit.
            self._sync_after_merge(rows, tx.is_interactive and refresh, data, server_values)
            if has_signals:
                tx.enqueue_post_commit(lambda: post_update.send(sender, instance=self, update_fields=update_fields))
            return None

        if not has_signals:
            await self._run_update(statement, variables, refresh, data, server_values)
            return None

        await pre_update.send(sender, instance=self, update_fields=update_fields)
        async with around_update.wrap(sender, instance=self, update_fields=update_fields):
            await self._run_update(statement, variables, refresh, data, server_values)
        await post_update.send(sender, instance=self, update_fields=update_fields)
        return None

    @_cache.invalidates("self")
    async def merge(
        self,
        tx: Transaction | None = None,
        server_values: Mapping[str, SurrealFunc] | None = None,
        extra_vars: Mapping[str, Any] | None = None,
        refresh: bool = True,
        **data: Any,
    ) -> Any:
        """
        Partial update of the model instance in the database.

        When ``tx`` is provided, the UPDATE…MERGE statement is buffered onto the
        transaction instead of being executed immediately.

        Note: ``tx``, ``server_values``, ``extra_vars`` and ``refresh`` are reserved keyword
        arguments for this method. A model field literally named like one of them cannot be
        merged by keyword; use a dict-unpacking workaround if needed (no realistic SurrealDB
        column carries those names).

        Emits pre_update, post_update, and around_update signals. In tx mode,
        ``around_update`` is skipped (the write is deferred to commit) and
        ``post_update`` only fires after a successful commit. The instance is resynced from
        the row the ``UPDATE`` itself returns — one round-trip, no follow-up read — and a
        merge that matches no record raises ``SurrealDbError``. A buffered transaction has no
        row yet, so ``data`` is applied to ``self`` directly at buffer time; a rollback will
        therefore leave the instance ahead of the database — caller must treat the instance
        as stale after a failed tx.

        Args:
            tx: Optional transaction the statement is buffered onto.
            server_values: Field name → :class:`~surreal_orm_lite.functions.SurrealFunc`,
                evaluated **by the server**. A server value overrides a ``data`` keyword
                of the same name. Providing it compiles the update as ``UPDATE … SET``
                (still a partial update: unlisted fields are untouched) and the returned
                row syncs the instance, so no extra ``refresh()`` round-trip is needed.
            extra_vars: Extra query variables the expressions reference, bound (never
                interpolated). Requires ``server_values``.
            refresh: ``True`` (default) resyncs the instance from the row the write returns.
                ``False`` compiles the write with ``RETURN NONE``: it still happens, but the
                server sends no row back and only the literal ``data`` is applied locally. Use
                it for fire-and-forget updates such as a presence ping or a counter bump, where
                the row is not worth its bytes.

                **The missing-record check goes with it.** That check *is* the returned row, so
                under ``refresh=False`` a merge against a record that no longer exists is a
                silent no-op instead of a ``SurrealDbError``, and a field computed by
                ``server_values`` keeps its stale value until the next ``refresh()``. Keep the
                default whenever you need to know the write landed.

        Example:
            >>> await user.merge(plan="pro", server_values={"updated_at": SurrealFunc("time::now()")})
            >>> await user.merge(last_seen=now, refresh=False)  # fire-and-forget
        """
        self._reject_snapshot_write("merge()")
        self._reject_computed_writes(data, "merge()")
        self._validate_server_values(server_values, extra_vars)
        if server_values:
            self._reject_computed_writes(server_values, "merge(server_values=)")
            return await self._merge_server(tx, server_values, extra_vars, data, refresh)

        sender = self.__class__
        data_set = dict(data.items())
        # Keyword arguments name Python fields; the MERGE payload names columns. Everything
        # that stays on this side of the wire — signals, the local apply — keeps `data_set`.
        wire_data = self._columns_for(data_set)
        record_id = self._record_id()

        if record_id is None:
            raise SurrealDbError(f"No Id for the data to merge: {data}")

        has_signals = pre_update.has_handlers(sender) or post_update.has_handlers(sender) or around_update.has_handlers(sender)

        if tx is not None:
            update_fields = list(data_set.keys())
            if has_signals:
                await pre_update.send(sender, instance=self, update_fields=update_fields)
            statement = "UPDATE $rid MERGE $data;" if refresh else "UPDATE $rid MERGE $data RETURN NONE;"
            rows = await tx.add(statement, {"rid": record_id, "data": wire_data})
            self._sync_after_merge(rows, tx.is_interactive and refresh, data_set, {})
            if has_signals:
                tx.enqueue_post_commit(lambda: post_update.send(sender, instance=self, update_fields=update_fields))
            return None

        if not has_signals:
            await self._merge_native(record_id, wire_data, data_set, refresh)
            return

        update_fields = list(data_set.keys())
        await pre_update.send(sender, instance=self, update_fields=update_fields)

        async with around_update.wrap(sender, instance=self, update_fields=update_fields):
            await self._merge_native(record_id, wire_data, data_set, refresh)

        await post_update.send(sender, instance=self, update_fields=update_fields)

    # ==================== Patch & atomic operations ====================

    @_cache.invalidates("self")
    async def patch(self, operations: list[dict[str, Any]], tx: Transaction | None = None) -> Self:
        """Apply a JSON Patch (RFC 6902) to this record.

        Backed by the SDK's native ``patch()`` (``UPDATE rid PATCH $data``). ``operations`` is
        a list of op dicts, e.g. ``{"op": "replace", "path": "/age", "value": 26}``. Requires
        an explicit id.

        Non-transactional and interactive-tx (3.x) calls apply the server's returned row to
        ``self``. In a buffered transaction (HTTP / SurrealDB 2.6.x) the result is unknown
        until commit, so ``self`` is left stale — treat the instance as needing ``refresh()``
        (same caveat as ``merge(tx=)``).

        Unlike ``merge``/``save`` this emits NO signals: it is a low-level atomic primitive.
        ``operations`` is validated then bound as data, never string-interpolated. Pointers may
        name a field either way — ``/password`` or its column ``/password_hash`` — and are
        rewritten to the column before they are sent.
        """
        self._reject_snapshot_write("patch()")
        validate_patch_operations(operations)
        self._reject_computed_patch(operations, "patch()")
        operations = self._columns_for_patch(operations)
        record_id = self._record_id()
        if record_id is None:
            raise SurrealDbError("patch() requires an explicit id (there is nothing to patch without one).")
        if tx is not None:
            rows = await tx.add("UPDATE $rid PATCH $data;", {"rid": record_id, "data": operations})
            self._apply_record(rows)  # interactive: applies; buffered: add() returns None → no-op (stale)
            return self
        client = await type(self)._connection_manager().get_client()
        record = await client.patch(record_id, _encoding.encode(operations))
        self._apply_record(record)
        return self

    async def _atomic_update(self, set_expr: str, variables: dict[str, Any], tx: Transaction | None, *, label: str) -> Self:
        """Run ``UPDATE $rid SET <set_expr>`` atomically and sync ``self`` with the result.

        Shared by the atomic helpers. ``$rid`` and every value in ``variables`` are bound
        (robust for any id type; anti-injection); only the validated ``set_expr`` is inlined.
        Non-tx and interactive-tx apply the returned row to ``self``; a buffered tx leaves
        ``self`` stale (the new value is computed server-side, unknown until commit).
        ``label`` is the calling method's name with its ``()``, for the snapshot refusal (v0.26.0).
        """
        self._reject_snapshot_write(label)
        record_id = self._record_id()
        if record_id is None:
            raise SurrealDbError("atomic operations require an explicit id.")
        vars_: dict[str, Any] = {"rid": record_id, **variables}
        statement = f"UPDATE $rid SET {set_expr};"
        if tx is not None:
            rows = await tx.add(statement, vars_)
            self._apply_record(rows)
            return self
        client = await type(self)._connection_manager().get_client()
        rows = await client.query(statement, _encoding.encode(vars_))
        self._apply_record(rows)
        return self

    @_cache.invalidates("self")
    async def atomic_append(self, field: str, value: Any, tx: Transaction | None = None) -> Self:
        """Atomically append ``value`` to an array ``field`` (duplicates allowed).

        Compiled to ``array::append`` — identical on SurrealDB 2.6.x and 3.x. For set
        semantics (skip if already present) use :meth:`atomic_set_add`. Emits no signals.
        """
        column = self._validate_atomic_field(field)
        return await self._atomic_update(
            f"{column} = array::append({column}, $value)", {"value": value}, tx, label="atomic_append()"
        )

    @_cache.invalidates("self")
    async def atomic_remove(self, field: str, value: Any, tx: Transaction | None = None) -> Self:
        """Atomically remove ALL occurrences of ``value`` from an array ``field``.

        Compiled to ``array::complement`` — removes every occurrence, identical on 2.6.x and
        3.x. (The ``-=`` operator is deliberately NOT used: it removes all occurrences on 3.x
        but only the first on 2.6.x.) Emits no signals.
        """
        column = self._validate_atomic_field(field)
        return await self._atomic_update(
            f"{column} = array::complement({column}, [$value])", {"value": value}, tx, label="atomic_remove()"
        )

    @_cache.invalidates("self")
    async def atomic_set_add(self, field: str, value: Any, tx: Transaction | None = None) -> Self:
        """Atomically add ``value`` to an array ``field`` only if not already present.

        Compiled to ``array::add`` (set semantics) — identical on 2.6.x and 3.x. (NOT the
        ``+=`` operator, which appends duplicates on both server lines.) Emits no signals.
        """
        column = self._validate_atomic_field(field)
        return await self._atomic_update(
            f"{column} = array::add({column}, $value)", {"value": value}, tx, label="atomic_set_add()"
        )

    @_cache.invalidates("self")
    async def atomic_increment(self, field: str, amount: Decimal | int | float = 1, tx: Transaction | None = None) -> Self:
        """Atomically add ``amount`` (default 1) to a numeric ``field``.

        Compiled to ``{field} += $amount`` — identical on 2.6.x and 3.x. Pass a negative
        ``amount`` to decrement. ``amount`` may be a ``decimal.Decimal`` for exact arithmetic
        (the value is bound, not interpolated); adding a ``Decimal`` to an int/float field
        coerces the stored field to SurrealDB ``decimal``. The instance then holds the value a
        read gives (#261): a ``float`` field a ``float``, whose rounding a later ``update()``
        writes back, so declare the field ``Decimal`` to keep the exact sum. Emits no signals.

        A duration amount raises ``TypeError`` (v0.23.0): SurrealDB 2.x ignores ``+=`` with a
        duration.
        """
        if isinstance(amount, (timedelta, SdkDuration, Duration)):
            raise TypeError(
                f"atomic_increment() adds a number, not a duration ({amount!r}): SurrealDB 2.x ignores += with a "
                "duration, so the result would differ between server lines. "
                f"Set the new value with merge({field}=…) instead."
            )
        column = self._validate_atomic_field(field)
        return await self._atomic_update(f"{column} += $amount", {"amount": amount}, tx, label="atomic_increment()")

    @staticmethod
    def _as_value_list(values: Any) -> list[Any]:
        """Coerce a ``*_many`` argument to a list, rejecting scalars (incl. str) with a clear error."""
        if isinstance(values, list):
            return values
        if isinstance(values, tuple):
            return list(values)
        raise ValueError(f"values must be a list of items, got {type(values).__name__}")

    @_cache.invalidates("self")
    async def atomic_append_many(self, field: str, values: list[Any], tx: Transaction | None = None) -> Self:
        """Atomically append every element of ``values`` to an array ``field`` (duplicates allowed).

        The list-valued counterpart of :meth:`atomic_append` — one round-trip instead of N.
        Compiled to ``array::concat`` (NOT ``array::append``, which would nest the list as a
        single element) — identical on 2.6.x and 3.x. An empty ``values`` is a safe no-op.
        Emits no signals.
        """
        column = self._validate_atomic_field(field)
        return await self._atomic_update(
            f"{column} = array::concat({column}, $values)",
            {"values": self._as_value_list(values)},
            tx,
            label="atomic_append_many()",
        )

    @_cache.invalidates("self")
    async def atomic_set_add_many(self, field: str, values: list[Any], tx: Transaction | None = None) -> Self:
        """Atomically add every element of ``values`` to an array ``field``, skipping any already present.

        The list-valued counterpart of :meth:`atomic_set_add` (set semantics; duplicates in
        ``values`` are also collapsed). Compiled to ``array::add`` — identical on 2.6.x and 3.x.
        An empty ``values`` is a safe no-op. Emits no signals.
        """
        column = self._validate_atomic_field(field)
        return await self._atomic_update(
            f"{column} = array::add({column}, $values)",
            {"values": self._as_value_list(values)},
            tx,
            label="atomic_set_add_many()",
        )

    @_cache.invalidates("self")
    async def atomic_remove_many(self, field: str, values: list[Any], tx: Transaction | None = None) -> Self:
        """Atomically remove ALL occurrences of every element of ``values`` from an array ``field``.

        The list-valued counterpart of :meth:`atomic_remove`. Compiled to ``array::complement``
        — identical on 2.6.x and 3.x. An empty ``values`` is a safe no-op. Emits no signals.
        """
        column = self._validate_atomic_field(field)
        return await self._atomic_update(
            f"{column} = array::complement({column}, $values)",
            {"values": self._as_value_list(values)},
            tx,
            label="atomic_remove_many()",
        )

    @_cache.invalidates("self", deletes=True)
    async def delete(self, tx: Transaction | None = None) -> None:
        """
        Delete the model instance from the database.

        When ``tx`` is provided, the DELETE statement is buffered onto the
        transaction instead of being executed immediately. The record id is bound
        (``$rid``), never written into the statement, so every id shape works —
        an array id such as ``follows:[User:alice, User:bob]`` included.

        Emits pre_delete, post_delete, and around_delete signals. In tx mode,
        ``around_delete`` is skipped (the write is deferred to commit) and
        ``post_delete`` only fires after a successful commit.
        """
        self._reject_snapshot_write("delete()")
        sender = self.__class__
        record_id = self._record_id()
        if record_id is None:
            raise SurrealDbError("Can't delete data, no id found.")

        has_signals = pre_delete.has_handlers(sender) or post_delete.has_handlers(sender) or around_delete.has_handlers(sender)

        if tx is not None:
            if not has_signals:
                await tx.add("DELETE $rid;", {"rid": record_id})
                return None
            await pre_delete.send(sender, instance=self)
            await tx.add("DELETE $rid;", {"rid": record_id})
            tx.enqueue_post_commit(lambda: post_delete.send(sender, instance=self))
            return None

        client = await type(self)._connection_manager().get_client()

        # Record delete() is STRICT: a missing record is an error on every server
        # version. SurrealDB 2.x returns falsy; 3.x raises NotFoundError. Both map
        # to SurrealDbError (preserves the existing "not found" contract).
        if not has_signals:
            try:
                deleted = await client.delete(record_id)
            except NotFoundError as e:
                raise SurrealDbError(f"Can't delete Record id -> '{record_id}' not found!") from e
            if not deleted:
                raise SurrealDbError(f"Can't delete Record id -> '{record_id}' not found!")
            logger.info(f"Record deleted -> {deleted}.")
            return

        await pre_delete.send(sender, instance=self)

        async with around_delete.wrap(sender, instance=self):
            try:
                deleted = await client.delete(record_id)
            except NotFoundError as e:
                raise SurrealDbError(f"Can't delete Record id -> '{record_id}' not found!") from e

            if not deleted:
                raise SurrealDbError(f"Can't delete Record id -> '{record_id}' not found!")

        await post_delete.send(sender, instance=self)

        logger.info(f"Record deleted -> {deleted}.")

    @model_validator(mode="after")
    def check_config(self) -> Self:
        """
        Check the model configuration.
        """

        if not self.get_index_primary_key() and not hasattr(self, "id"):
            raise SurrealDbError(  # pragma: no cover
                "Can't create model, the model needs either 'id' field or primary_key in 'model_config'."
            )

        return self

    # ==================== Relations & Graph ====================

    @staticmethod
    def _check_thing_table(record_id: RecordID) -> None:
        """Refuse a ``RecordID`` whose table name is not an identifier, before it becomes text.

        Raises:
            ValueError: the table name is not an identifier (letters, digits, underscores; not
                starting with a digit).
        """
        if not isinstance(record_id.table_name, str) or not VALID_ALIAS_PATTERN.fullmatch(record_id.table_name):
            raise ValueError(
                f"Invalid record identifier: table name {record_id.table_name!r} must be an identifier "
                "(letters, digits, underscores; not starting with a digit)."
            )

    def _get_thing(self) -> str:
        """Return the ``table:id`` text of this instance.

        The ORM's own graph statements bind the record id instead (:meth:`_graph_source`); this
        text form is kept for code that reads it. A ``RecordID`` id must name an identifier table.
        """
        id_val = self.get_id()
        if id_val is None:
            raise SurrealDbError("Cannot use relations on an unsaved model (no id).")
        if isinstance(id_val, RecordID):
            # A RecordID from the DB is already typed; its string form may contain SurrealQL
            # escaping (e.g. "Table:⟨1⟩") which validate_thing intentionally rejects. The SDK's
            # string form does not escape the table name, though, so that one is checked.
            self._check_thing_table(id_val)
            return str(id_val)
        table = self.get_table_name()
        validate_thing(f"{table}:{id_val}")
        # save() stores a str id as a *string* record id, but an unquoted digit run in
        # SurrealQL is an integer one — so `M:1` addressed a record the ORM never wrote, and
        # every relation built from this thing silently pointed at nothing. format_record_id
        # keys off the value's Python type, so `id: int` keeps the integer form that
        # _record_id() (and therefore save()) uses (issue #156).
        return f"{table}:{format_record_id(id_val)}"

    @staticmethod
    def _resolve_target_thing(target: "BaseSurrealModel | str | RecordID") -> str:
        """Resolve a target to a ``table:id`` string.

        A ``"table:id"`` string is quoted by the same rule as a model's own id, so
        ``M:1`` and ``M(id="1")`` name the same record; pass a ``RecordID`` (or the model
        instance) to target an integer record id. An id already written backtick-quoted is kept
        verbatim. A ``RecordID`` must name an identifier table. ``relate()`` binds its target
        instead (:meth:`_graph_target`); this text form is kept for code that reads it.
        """
        if isinstance(target, RecordID):
            BaseSurrealModel._check_thing_table(target)
            return f"{target.table_name}:{format_record_id(target.id)}"
        if isinstance(target, str):
            validate_thing(target)
            table, _, raw_id = target.partition(":")
            if raw_id.startswith("`"):
                return target
            return f"{table}:{format_record_id(raw_id)}"
        if isinstance(target, BaseSurrealModel):
            return target._get_thing()
        raise TypeError(f"target must be a BaseSurrealModel instance or 'table:id' string, got {type(target).__name__}")

    @staticmethod
    def _edge_name(edge: object) -> str:
        """Return the edge table an ``edge`` argument names (v0.22.0).

        An edge model class names its ``get_table_name()`` (an identifier, checked when the class
        was defined); a string passes :func:`~surreal_orm_lite.utils.validate_edge_name` and is
        returned as is. Shared by ``relate()`` and the graph helpers, so they all refuse the same
        arguments the same way, before any call.

        Raises:
            TypeError: a model class that is not an edge model, or anything that is neither a
                class nor a string.
            ValueError: a string that is not a valid edge name.
        """
        if isinstance(edge, str):
            validate_edge_name(edge)
            return edge
        if isinstance(edge, type) and issubclass(edge, BaseSurrealModel):
            if not edge.__surreal_edge__:
                raise TypeError(
                    f"{edge.__name__} is not an edge model: subclass BaseSurrealEdge, or pass the edge table name as a string."
                )
            return edge.get_table_name()
        raise TypeError(f"edge must be an edge model class or an edge table name, got {type(edge).__name__}")

    def _graph_source(self) -> RecordID:
        """Return this record's id, bound as the source of a graph statement (v0.22.0).

        Bound, never interpolated, so the id needs no identifier check: a node saved as
        ``joe@x.com`` relates like it saves.

        Raises:
            SurrealDbError: the instance has no id yet.
        """
        record_id = self._record_id()
        if record_id is None:
            raise SurrealDbError("Cannot use relations on an unsaved model (no id).")
        return record_id

    @staticmethod
    def _graph_target(target: object) -> RecordID:
        """Return the record id a graph ``target`` names, to bind (v0.22.0).

        A model instance gives its record id (an unsaved one raises), a ``RecordID`` is returned
        as is, and a ``"table:id"`` string is read by
        :func:`~surreal_orm_lite.utils.coerce_endpoint` — the rule of the edge endpoints, so
        ``"M:1"`` names the string id ``"1"``, as ``M(id="1")`` does, and ``"M:`a-b`"`` the id
        ``a-b``. A string without ``:`` is refused.

        Raises:
            SurrealDbError: an unsaved instance.
            ValueError: a malformed ``"table:id"`` string.
            TypeError: any other type.
        """
        if isinstance(target, BaseSurrealModel):
            return target._graph_source()
        if isinstance(target, RecordID):
            return target
        if isinstance(target, str):
            return coerce_endpoint(target)
        raise TypeError(
            f"target must be a BaseSurrealModel instance, a RecordID or a 'table:id' string, got {type(target).__name__}"
        )

    @staticmethod
    def _merge_edge_fields(
        data: Mapping[str, Any] | None,
        edge_data: Mapping[str, Any],
        to_name: Callable[[str], str],
    ) -> dict[str, Any]:
        """Merge ``relate()``'s ``data=`` with its keyword edge fields (v0.22.0).

        ``to_name`` reads a key as the field it names: ``to_py_field`` on the typed path, so a
        column alias (``memo``) and its Python name (``note``) are the same field, and the
        identity on the string path. Every later check runs on the merged mapping.

        Raises:
            TypeError: a field given both in ``data=`` and as a keyword argument.
        """
        given = dict(data or {})
        both = {to_name(key) for key in given} & {to_name(key) for key in edge_data}
        if both:
            raise TypeError(
                f"relate() got edge field(s) {sorted(both)} both in data= and as keyword arguments; pass each one once."
            )
        return {**given, **edge_data}

    @overload
    async def relate(
        self,
        edge: type[_E],
        target: "BaseSurrealModel | RecordID | str",
        reverse: bool = False,
        tx: Transaction | None = None,
        *,
        data: Mapping[str, Any] | None = None,
        **edge_data: Any,
    ) -> _E: ...

    @overload
    async def relate(
        self,
        edge: str,
        target: "BaseSurrealModel | RecordID | str",
        reverse: bool = False,
        tx: Transaction | None = None,
        *,
        data: Mapping[str, Any] | None = None,
        **edge_data: Any,
    ) -> list[dict[str, Any]]: ...

    @_cache.invalidates("edge")
    async def relate(
        self,
        edge: "str | type[BaseSurrealEdge]",
        target: "BaseSurrealModel | RecordID | str",
        reverse: bool = False,
        tx: Transaction | None = None,
        *,
        data: Mapping[str, Any] | None = None,
        **edge_data: Any,
    ) -> "BaseSurrealEdge | list[dict[str, Any]]":
        """
        Create a graph relation (an edge) between this record and the target.

        ``edge`` is an edge model class or an edge table name:

        - **An edge model class** (a :class:`~surreal_orm_lite.edge.BaseSurrealEdge` subclass,
          v0.22.0) — the edge is validated, created through the edge's ``save()``
          (``INSERT RELATION``, the ``save`` signals) and returned, typed: ``id`` is the
          server's ``RecordID``, ``in_``/``out`` the two records. The edge fields may not name
          an endpoint (``in``/``in_``/``out``), a computed field or a field the model does not
          declare (unless it sets ``extra="allow"``); this is checked before any call. A
          ``server_fields`` field named there is written instead of being left to the server.
        - **An edge table name** — the v0.6.0 ``RELATE`` (any target table), the edge fields
          written with ``SET``; returns the rows the database sent back.

        In both forms the source and the target are bound as record ids (only the edge name and
        the field names are written into the statement), so a node saved with an id such as
        ``joe@x.com`` relates like it saves.

        Parameters follow the full SurrealDB-ORM's order, each by position or by keyword:
        ``relate(edge, target, reverse, tx)`` is ``relate(edge, target, reverse=…, tx=…)``.
        ``data`` is keyword-only. Edge fields go in ``data=`` (lite spelling) or as keyword
        arguments (full ORM spelling), or both: a field given both ways is a ``TypeError``. A
        field named like a parameter of this method — ``self``, ``edge``, ``target``,
        ``data``, ``reverse`` or ``tx`` — cannot be a keyword argument (Python binds it to
        the parameter): pass it in ``data=`` (``data={"reverse": True}``).

        Args:
            edge: An edge model class, or the relation/edge table name (e.g. ``"follows"``).
            target: A saved model instance, a ``RecordID`` or a ``"table:id"`` string.
            reverse: ``False`` (default) relates ``self -> edge -> target``; ``True`` relates
                ``target -> edge -> self`` (the typed edge's ``in_`` is then the target).
            tx: Optional transaction. On an interactive one (WebSocket + SurrealDB 3.x) the
                edge is written at once and comes back as usual; on a buffered one it is
                written at commit: the typed form returns the edge with ``id`` ``None``, the
                string form ``[]``.
            data: Optional fields stored on the edge.
            **edge_data: More fields stored on the edge, merged with ``data``.

        Returns:
            The created edge (edge model class), or the created relation rows (table name).

        Raises:
            TypeError: ``edge`` is a model class that is not an edge model, or neither a class
                nor a string; ``target`` is of another type; a field is given both in ``data``
                and as a keyword argument.
            ValueError: a malformed name or ``"table:id"`` string; the fields name an endpoint
                or a computed field (typed form).
            SurrealDbError: an unsaved source or target; the fields name an undeclared field
                (typed form); the write is refused; this record was read with ``as_of()`` (a
                read-only snapshot, v0.26.0); this record, a model target or the edge model run on
                two connections (v0.29.0: a relation is stored in one database), before any request
                or signal.
            pydantic.ValidationError: the fields are invalid for the edge model (nothing
                written).

        Example::

            edge = await user.relate(Purchased, product, quantity=2)  # -> Purchased
            edge = await user.relate(Purchased, product, data={"quantity": 2})  # lite spelling
            await user.relate("follows", other_user, since="2025-01-01")
            await table.relate("created", creator, reverse=True)  # creator -> created -> table
            await table.relate("created", creator, True)  # same call, the full ORM's order
            async with SurrealDBConnectionManager.transaction() as tx:
                await user.relate(Follows, other_user, tx=tx)
        """
        self._reject_snapshot_write("relate()")
        self._check_relation("relate", self._model_of(target), self._edge_model(edge))
        if isinstance(edge, type) and issubclass(edge, BaseSurrealModel) and edge.__surreal_edge__:
            fields = self._merge_edge_fields(data, edge_data, edge.to_py_field)
            return await self._relate_typed(edge, target, fields, reverse, tx)

        edge_name = self._edge_name(edge)
        fields = self._merge_edge_fields(data, edge_data, str)  # a column name is the field name
        # Both record ids are bound, as in the typed form and the other graph helpers: only the
        # validated edge name and field names are written into the statement.
        variables: dict[str, Any] = {"src": self._graph_source(), "dst": self._graph_target(target)}
        start, end = ("$dst", "$src") if reverse else ("$src", "$dst")

        set_parts: list[str] = []
        for i, (key, value) in enumerate(fields.items()):
            validate_field_name(key, "relation data field")
            var_name = f"_rd{i}"
            set_parts.append(f"{key} = ${var_name}")
            variables[var_name] = value
        set_clause = (" SET " + ", ".join(set_parts)) if set_parts else ""
        query = f"RELATE {start}->{edge_name}->{end}{set_clause};"

        if tx is not None:
            rows = await tx.add(query, variables)
            return rows if isinstance(rows, list) else []

        client = await type(self)._connection_manager().get_client()
        result = await client.query(query, _encoding.encode(variables))
        return result if isinstance(result, list) else []

    async def _relate_typed(
        self,
        edge: type[_E],
        target: object,
        fields: Mapping[str, Any],
        reverse: bool,
        tx: Transaction | None,
    ) -> _E:
        """``relate(EdgeModel, …)``: validate, create through the edge's ``save()``, return it.

        Every check runs before any call: the source and the target (bound as record ids), then
        ``fields`` — keys in either vocabulary (Python name or column), translated to Python
        names — and the model's own validation. ``reverse`` swaps the endpoints.
        """
        source = self._graph_source()
        destination = self._graph_target(target)
        names = [edge.to_py_field(key) for key in fields]
        endpoints = [key for key, name in zip(fields, names, strict=True) if name in edge._write_protected_fields()]
        if endpoints:
            raise ValueError(
                f"relate(): `in`/`out` are the endpoints of edge `{edge.__name__}`: relate() takes them from "
                f"this record and the target, so its edge fields cannot name them (got {', '.join(endpoints)})."
            )
        edge._reject_computed_writes(names, "relate()")
        edge._reject_undeclared_writes(names)
        start, end = (destination, source) if reverse else (source, destination)
        instance = edge.model_validate({**fields, "in": start, "out": end})
        return await instance._save_naming(names, tx=tx)

    def _check_relation(self, method: str, *models: type | None) -> None:
        """Refuse the relation helper *method* when *models* run on another connection than this record (v0.29.0).

        *models* are the target's model, the edge model and ``model_class``, in the order the error names them;
        ``None`` (a ``RecordID`` or ``"table:id"`` target, an edge table name) is skipped. Connection names are
        compared, before any signal or request.

        :raises SurrealDbError: two connections (``routing.check_same_connection()``).
        """
        others = [model for model in models if model is not None]
        if others:
            check_same_connection(f"{type(self).__name__}.{method}()", type(self), *others)

    @staticmethod
    def _model_of(target: object) -> type | None:
        """The model of a relation target that is a model instance, else ``None`` (a ``RecordID``, a string)."""
        return type(target) if isinstance(target, BaseSurrealModel) else None

    @staticmethod
    def _edge_model(edge: object) -> type | None:
        """*edge* when it is an edge model class, else ``None`` (an edge table name, or a value refused later)."""
        if isinstance(edge, type) and issubclass(edge, BaseSurrealModel) and edge.__surreal_edge__:
            return edge
        return None

    @staticmethod
    def _edge_names(edge: object) -> list[str]:
        """The edge tables a ``remove_all_relations()`` argument names (v0.22.0).

        A ``list`` or ``tuple`` names each of its items (de-duplicated, in order), anything else
        one edge; every item goes through :meth:`_edge_name`.

        Raises:
            ValueError: an empty list or tuple, or an invalid edge name.
            TypeError: an item that is neither an edge model class nor a string.
        """
        if isinstance(edge, (list, tuple)):
            names = list(dict.fromkeys(BaseSurrealModel._edge_name(item) for item in edge))
            if not names:
                raise ValueError("remove_all_relations() needs at least one edge")
            return names
        return [BaseSurrealModel._edge_name(edge)]

    @staticmethod
    def _check_direction(direction: str) -> None:
        """Refuse a graph direction other than ``"out"``, ``"in"`` or ``"both"``."""
        if direction not in ("out", "in", "both"):
            raise ValueError(f"direction must be 'out', 'in', or 'both', got '{direction}'")

    @classmethod
    async def _run_graph_delete(cls, statement: str, variables: dict[str, Any], tx: Transaction | None) -> None:
        """Run a graph-path ``DELETE`` now, on this model's connection, or queue it on ``tx``.

        Outside a transaction a ``NotFoundError`` is a silent no-op (the v0.7.0 contract: on 3.x
        it is only raised for a source record whose own table does not exist). Inside one the
        transaction layer's errors apply, so such a statement fails the transaction.
        """
        if tx is not None:
            await tx.add(statement, variables)
            return
        client = await cls._connection_manager().get_client()
        with contextlib.suppress(NotFoundError):
            await client.query(statement, _encoding.encode(variables))

    @_cache.invalidates("edge")
    async def remove_relation(
        self,
        edge: "str | type[BaseSurrealEdge]",
        target: "BaseSurrealModel | RecordID | str",
        reverse: bool = False,
        tx: Transaction | None = None,
    ) -> None:
        """
        Remove the relations of one edge table between this record and the target.

        Parameters follow the full SurrealDB-ORM's order, each by position or by keyword:
        ``remove_relation(edge, target, reverse, tx)`` is
        ``remove_relation(edge, target, reverse=…, tx=…)``.

        The statement is a graph-path ``DELETE`` with bound record ids
        (``DELETE $src->edge WHERE out = $dst``): it removes exactly the edges graph traversal
        sees — a plain record that merely carries ``in``/``out`` fields is not an edge and is
        not matched. An edge table that was never created is a no-op, also in a transaction.

        ``target`` is one of:

        - a saved model instance, a ``RecordID`` or a ``"table:id"`` string — that record;
        - a **bare record id** (a string without ``:``, e.g. ``"abc123"``) — bound verbatim and
          compared **as a string** with the id of the edge's ``out`` (``in`` with
          ``reverse=True``), so it matches that id in **any** table linked through the edge
          table (``Player:abc123`` and ``Bot:abc123`` alike) and never an integer record id
          (``"7"`` does not match ``Player:7``). Pass the instance or a ``RecordID`` to pin the
          table or to reach an integer id. An empty or blank string raises ``ValueError``.

        Args:
            edge: An edge model class, or the relation/edge table name.
            target: The other record, as above.
            reverse: ``False`` (default) removes ``self -> edge -> target``; ``True`` removes
                ``target -> edge -> self``.
            tx: Optional transaction. On an interactive one (WebSocket + SurrealDB 3.x) the
                statement runs now: the removal is visible to ``objects(tx=)`` reads and a
                rollback restores the edges. On a buffered one (2.x, or HTTP) it is queued,
                applied at commit and rolled back with the batch. Inside a transaction, a source
                record whose own table does not exist fails the statement on 3.x (and so the
                transaction); outside one it is a silent no-op.

        Raises:
            TypeError: ``edge`` is not an edge model class or a string; ``target`` is of
                another type.
            ValueError: an invalid edge name, a malformed ``"table:id"`` string, or an empty or
                blank target.
            SurrealDbError: an unsaved source or target; this record was read with ``as_of()`` (a
                read-only snapshot, v0.26.0); this record, a model target or the edge model run on two
                connections (v0.29.0), before any request.

        Example::

            await user.remove_relation("follows", other_user)
            await user.remove_relation(Follows, "User:bob")
            await table.remove_relation("created", creator, reverse=True)  # creator -> table
            await table.remove_relation("created", creator, True)  # same call, positional
            await table.remove_relation("has_player", "abc123")  # bare id, any table
            async with SurrealDBConnectionManager.transaction() as tx:
                await user.remove_relation(Follows, other_user, tx=tx)
        """
        self._reject_snapshot_write("remove_relation()")
        self._check_relation("remove_relation", self._model_of(target), self._edge_model(edge))
        edge_name = self._edge_name(edge)
        source = self._graph_source()
        arrow, column = ("<-", "in") if reverse else ("->", "out")
        if isinstance(target, str) and ":" not in target:
            if not target.strip():
                raise ValueError(
                    "remove_relation() target must not be empty or blank: pass a model instance, a RecordID, "
                    "a 'table:id' string or a bare record id."
                )
            statement = f"DELETE $src{arrow}{edge_name} WHERE record::id({column}) = $id;"
            variables: dict[str, Any] = {"src": source, "id": target}
        else:
            statement = f"DELETE $src{arrow}{edge_name} WHERE {column} = $dst;"
            variables = {"src": source, "dst": self._graph_target(target)}
        await self._run_graph_delete(statement, variables, tx)

    @_cache.invalidates("edge")
    async def remove_all_relations(
        self,
        edge: "str | type[BaseSurrealEdge] | Sequence[str | type[BaseSurrealEdge]]",
        direction: str = "out",
        tx: Transaction | None = None,
    ) -> None:
        """
        Remove every relation of the given edge table(s) from and/or to this record.

        Parameters follow the full SurrealDB-ORM's order, each by position or by keyword:
        ``remove_all_relations(edge, direction, tx)`` is
        ``remove_all_relations(edge, direction=…, tx=…)``.

        One graph-path ``DELETE`` with a bound record id (``DELETE $src->(e1, e2)``): it removes
        exactly the edges graph traversal sees — a plain record that merely carries
        ``in``/``out`` fields is not an edge and is not matched — a self-loop once, and an edge
        table that was never created is a no-op, also in a transaction.

        Args:
            edge: An edge model class or an edge table name, or a list (or tuple) of them.
            direction: ``"out"`` removes outgoing relations (default), ``"in"`` incoming ones,
                ``"both"`` every relation involving this record.
            tx: Optional transaction. On an interactive one (WebSocket + SurrealDB 3.x) the
                statement runs now: the removal is visible to ``objects(tx=)`` reads and a
                rollback restores the edges. On a buffered one (2.x, or HTTP) it is queued,
                applied at commit and rolled back with the batch. Inside a transaction, a source
                record whose own table does not exist fails the statement on 3.x (and so the
                transaction); outside one it is a silent no-op.

        Raises:
            ValueError: an invalid direction or edge name, or an empty list.
            TypeError: an edge that is neither an edge model class nor a string.
            SurrealDbError: an unsaved record, or one read with ``as_of()`` (a read-only snapshot,
                v0.26.0); an edge model of another connection than this record's (v0.29.0), before any
                request.

        Example::

            await user.remove_all_relations("follows", direction="out")
            await user.remove_all_relations(Follows, "both")  # positional direction
            await table.remove_all_relations(["has_player", HasAction, "has_state"])
            async with SurrealDBConnectionManager.transaction() as tx:
                await user.remove_all_relations(["likes", "blocks"], direction="both", tx=tx)
        """
        self._reject_snapshot_write("remove_all_relations()")
        self._check_direction(direction)
        edges = edge if isinstance(edge, (list, tuple)) else (edge,)
        self._check_relation("remove_all_relations", *(self._edge_model(item) for item in edges))
        names = ", ".join(self._edge_names(edge))
        source = self._graph_source()
        paths = {"out": f"$src->({names})", "in": f"$src<-({names})", "both": f"$src->({names}), $src<-({names})"}
        await self._run_graph_delete(f"DELETE {paths[direction]};", {"src": source}, tx)

    async def get_related(
        self,
        edge: "str | type[BaseSurrealEdge]",
        direction: str = "out",
        model_class: type["BaseSurrealModel"] | None = None,
    ) -> list[Any]:
        """
        Get related records through a relation edge.

        Parameters follow the full SurrealDB-ORM's order, each by position or by keyword:
        ``get_related(edge, direction, model_class)`` is
        ``get_related(edge, direction=…, model_class=…)``.

        Args:
            edge: An edge model class, or the relation/edge table name.
            direction: ``"out"`` for outgoing relations (default), ``"in"`` for incoming ones,
                ``"both"`` for the out-neighbours followed by the in-neighbours (one query; a
                neighbour linked by k edges appears k times, this record only through a
                self-loop, once per direction; the order inside each group is unspecified).
            model_class: Optional model class to deserialize results into.

        Returns:
            A list of related records: model instances if ``model_class`` is given, otherwise
            the neighbours' record ids (``RecordID``).

        Raises:
            ValueError: an invalid direction or edge name.
            TypeError: ``edge`` is not an edge model class or a string.
            SurrealDbError: an unsaved record; the edge model or ``model_class`` run on another connection
                than this record (v0.29.0: the traversal reads one database), before any request.

        Example::

            following = await user.get_related("follows", direction="out", model_class=User)
            followers = await user.get_related("follows", "in", User)  # positional
            everyone = await user.get_related(Follows, direction="both", model_class=User)
        """
        self._check_direction(direction)
        self._check_relation("get_related", self._edge_model(edge), model_class)
        edge_name = self._edge_name(edge)
        source = self._graph_source()

        # With a model class the caller wants whole records, so project them in the traversal
        # itself (``->edge->?.*``) instead of fetching ids and then selecting them: same
        # result, one round-trip instead of two (issue #156). Verified on 2.6.x and 3.x.
        projection = "?.*" if model_class is not None else "?"
        outgoing = f"->{edge_name}->{projection}"
        incoming = f"<-{edge_name}<-{projection}"
        # "both": the full ORM's two queries in one; `<->edge<->?` would return both ends of
        # every edge, so this record once per edge (measured on 2.7.0, 3.1.5 and 3.3.3).
        selected = {"out": outgoing, "in": incoming, "both": f"array::concat({outgoing}, {incoming})"}[direction]
        query = f"SELECT VALUE {selected} FROM ONLY $src;"

        client = await type(self)._connection_manager().get_client()
        results = await client.query(query, _encoding.encode({"src": source}))

        if not isinstance(results, list) or len(results) == 0:
            return []

        # Results from SELECT VALUE ->edge->? are a flat list of record IDs/objects
        records = results

        if model_class is not None:
            # If results are RecordIDs, we need to SELECT them
            if records and isinstance(records[0], RecordID):
                # RecordIDs come from the database (typed/trusted) — bind them as a
                # query variable instead of string-interpolating, which both avoids
                # injection risk and the SurrealQL-escaped string form (e.g. "Table:⟨1⟩")
                # that validate_thing would reject.
                fetch_query = "SELECT * FROM $ids;"
                records = await client.query(fetch_query, _encoding.encode({"ids": list(records)}))
                if not isinstance(records, list):
                    return []

            try:
                return model_class.from_db(records)  # type: ignore
            except (ValueError, TypeError):
                return records

        return records

    async def traverse(self, path: str) -> list[Any]:
        """
        Execute a graph traversal from this record.

        The path is validated and written into the statement; this record's id is bound, so a
        node whose id is not an identifier (``joe@x.com``) traverses like it saves.

        Args:
            path: A SurrealQL graph traversal path starting with ``->`` or ``<-``.

        Returns:
            A list of records found by the traversal.

        Raises:
            ValueError: an invalid path.
            SurrealDbError: an unsaved record.

        Example::

            # Friends of friends
            fof = await user.traverse("->follows->User->follows->User")
        """
        validate_graph_path(path)
        source = self._graph_source()

        query = f"SELECT VALUE {path} FROM ONLY $src;"
        client = await type(self)._connection_manager().get_client()
        results = await client.query(query, _encoding.encode({"src": source}))

        if isinstance(results, list):
            return results
        return []

    @classmethod
    def objects(cls, tx: Transaction | None = None) -> Any:
        """
        Return a QuerySet for the model class.

        When ``tx`` is provided, the QuerySet's reads and bulk operations participate in
        that transaction (reads require an interactive transaction — WS + SurrealDB 3.x). A transaction of
        another connection than the model's (v0.29.0) raises ``SurrealDbError`` here, before anything is sent.
        """
        from .query_set import QuerySet

        if tx is not None:
            check_transaction(cls, tx, f"{cls.__name__}.objects(tx=)")
        qs = QuerySet(cls)
        if tx is not None:
            qs._tx = tx
        return qs

    @classmethod
    async def raw_query(
        cls,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> list[Self] | list[dict[str, Any]]:
        """
        Execute a raw SurrealQL query and return the results.

        This method allows executing arbitrary SurrealQL queries directly against
        the database. It's useful for complex queries that can't be expressed
        using the QuerySet API.

        Args:
            query: The SurrealQL query string to execute.
            variables: Optional dictionary of variables to substitute into the query.
                      Use $variable_name syntax in the query string.

        Returns:
            list[Self] | list[dict]: A list of model instances if the results match
            the model schema, otherwise a list of dictionaries.

        Example:
            ```python
            # Simple query
            users = await User.raw_query("SELECT * FROM User WHERE age > 21")

            # With variables (safe from injection)
            users = await User.raw_query(
                "SELECT * FROM User WHERE status = $status AND age > $min_age",
                variables={"status": "active", "min_age": 18}
            )

            # Complex graph query
            results = await User.raw_query('''
                SELECT *, ->purchased->Product AS products
                FROM User
                WHERE id = $user_id
            ''', variables={"user_id": "user:123"})
            ```
        """
        warn_on_multiple_statements(query)

        # Sent verbatim: this is the caller's own SurrealQL, so the ORM does not rewrite it
        # (unquoting "$word" occurrences used to corrupt string literals — issue #156).
        client = await cls._connection_manager().get_client()
        results = await client.query(query, _encoding.encode(variables or {}))

        # SDK 2.0 returns the rows list directly from query()
        if isinstance(results, list):
            try:
                return cls.from_db(results)  # type: ignore
            except (ValueError, TypeError, ValidationError):
                # If validation fails, return raw dicts
                return results

        return []

    # ==================== Stored Function Calls (v0.15.0) ====================

    @classmethod
    async def call_function(
        cls,
        function: str,
        args: Sequence[Any] | None = None,
        *,
        params: Mapping[str, Any] | None = None,
        return_type: Any = None,
        tx: Transaction | None = None,
    ) -> Any:
        """Call a custom server-side function declared with ``DEFINE FUNCTION fn::…``.

        A convenience shortcut for code organised around models; it delegates verbatim to
        :meth:`SurrealDBConnectionManager.call_function`, which documents the arguments and
        the transaction semantics, on the model's connection (:meth:`get_connection_name`,
        v0.29.0). A stored function is **not** bound to a table, so calling it through one model
        rather than another of the same connection changes nothing::

            acquired = await GameTable.call_function("fn::acquire_lock", [table_id, pod_id])

        A ``tx`` of another connection than the model's raises ``SurrealDbError`` before anything.
        """
        check_transaction(cls, tx, f"{cls.__name__}.call_function()")
        return await cls._connection_manager().call_function(function, args, params=params, return_type=return_type, tx=tx)
