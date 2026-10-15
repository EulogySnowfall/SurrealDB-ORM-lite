"""Internal: the read side of the SDK boundary (#235), the counterpart of ``encoding.py``.

Not part of the public API — nothing here is exported by ``surreal_orm_lite``.

The surrealdb SDK 2.0.0 decodes every response in its connection modules with its CBOR
``tag_decoder``, which raises on four kinds of values SurrealDB returns:

* a zero duration (CBOR tag 14 with an empty compact form): ``IndexError``;
* a SurrealDB 3.x native set (tag 56): ``BufferError``;
* a ``LineString`` of fewer than 2 points, alone or inside another geometry: ``TypeError``;
* a ``Polygon`` ring of fewer than 4 points: ``InvalidGeometryError``.

One such value fails the whole response. Over WebSocket it also ends the SDK's receive loop, which
cancels every request in flight. ``read_tag_hook`` wraps the SDK's ``tag_decoder``: every tag goes to
it first, and only these four failures get a value instead of the error — the SDK's
``Duration(0)``, a Python ``set`` (a ``list`` when an item is unhashable or two items are equal in
Python, like ``1`` and ``true``) and the geometry's GeoJSON ``dict`` (``{"type": …, "coordinates":
…}``, or ``"geometries"`` for a collection), which a geometry field validates or refuses clearly.
Any other failure is raised as the SDK raises it (a degenerate member beside a member of another
kind, which SurrealDB never sends, raises ``TypeError``), and every value the SDK decodes comes back
unchanged.

``install(client)`` gives the hook to one client, the one ``ConnectionManager._open_client`` has just
built. SDK 2.0.0 has no per-client hook: each connection module binds ``decode`` at module level and
calls it in one method (``_recv_task`` over WebSocket, ``_send`` over HTTP and embedded). That method
is set on the instance as a copy of the SDK's own function — the same code object — whose module
globals are a copy of its module's with ``decode`` replaced by the SDK's ``decode`` run with
``read_tag_hook`` as its ``tag_decoder``. Nothing is patched, forked or vendored: the code that runs is
the SDK's, its modules and classes are untouched, and a client the application builds itself with
``AsyncSurreal`` keeps the SDK's behaviour.

The hook is installed only when the installed SDK cannot decode one of these values (``needed()``:
capability probes on hand-built CBOR, run once, no I/O, never a version comparison) and only when the
SDK's layout is the one described above (``available()``).
"""

from __future__ import annotations

import functools
import types
from collections.abc import Callable
from typing import Any, Final

from . import _sdk

__all__ = [
    "TAG_SET",
    "active",
    "available",
    "install",
    "needed",
    "read_tag_hook",
    "sdk_decodes_degenerate_geometries",
    "sdk_decodes_native_sets",
    "sdk_decodes_zero_duration",
]

# SDK 3.0 (#205): the protocol tag of a SurrealDB 3.x native set, which SDK 2.0.0 does not define;
# 3.0.0b10 decodes it (``SurrealSet``). Remove with the set fallback below.
TAG_SET: Final = 56

#: The GeoJSON ``type`` of each geometry tag of the SurrealDB protocol.
_GEOMETRY_TYPES: Final[dict[int, str]] = {
    88: "Point",
    89: "LineString",
    90: "Polygon",
    91: "MultiPoint",
    92: "MultiLineString",
    93: "MultiPolygon",
    94: "GeometryCollection",
}

#: The geometries made of members, by GeoJSON ``type``: the SDK class, the attribute listing its
#: members and the members' GeoJSON ``type``. A ``GeometryCollection`` holds members of any type.
_COMPOSITES: Final[dict[str, tuple[type[Any], str, str]]] = {
    "LineString": (_sdk.SdkGeometryLine, "geometry_points", "Point"),
    "Polygon": (_sdk.SdkGeometryPolygon, "geometry_lines", "LineString"),
    "MultiPoint": (_sdk.SdkGeometryMultiPoint, "geometry_points", "Point"),
    "MultiLineString": (_sdk.SdkGeometryMultiLine, "geometry_lines", "LineString"),
    "MultiPolygon": (_sdk.SdkGeometryMultiPolygon, "geometry_polygons", "Polygon"),
}

# SDK 3.0 (#205): the SDK 2.0.0 methods that decode a response, by name: ``_recv_task`` (WebSocket)
# and ``_send`` (HTTP, embedded). Re-check against the stable SDK's connection modules; the hook is
# not installed when none of a client's methods reads the SDK's ``decode`` as a module global.
_DECODING_METHODS: Final = ("_recv_task", "_send")

_UNHANDLED: Final = object()


# --- the tag hook --------------------------------------------------------------------------------


def read_tag_hook(decoder: Any, tag: Any) -> Any:
    """The SDK's ``tag_decoder``, except that the four values SDK 2.0.0 refuses are read.

    *tag* goes to the SDK's ``tag_decoder`` first. When that raises, a zero duration, a native set
    or a degenerate geometry is read (see the module docstring); anything else re-raises the SDK's
    error. A geometry the SDK built around a member read as GeoJSON (a collection or a multi-line
    holding a degenerate line) is returned as GeoJSON too, since an SDK geometry cannot hold it; one
    whose members are of another kind, a value SurrealDB never sends, raises ``TypeError``.
    """
    sdk_tag_decoder: Callable[[Any, Any], Any] = _sdk.cbor_tag_decoder
    try:
        value = sdk_tag_decoder(decoder, tag)
    except Exception:
        fallback = _fallback(tag)
        if fallback is _UNHANDLED:
            raise
        return fallback
    # SDK 3.0 (#205): keep while the SDK does not decode degenerate geometries (3.0.0b10 does not):
    # SDK 2.0.0's multi-geometries and collections accept any member, a GeoJSON dict included.
    if tag.tag in _GEOMETRY_TYPES and isinstance(tag.value, (list, tuple)) and any(isinstance(m, dict) for m in tag.value):
        return _geojson(tag)
    return value


def _fallback(tag: Any) -> Any:
    """What the hook reads for a tag the SDK refused, or ``_UNHANDLED`` to re-raise the SDK's error."""
    number, value = tag.tag, tag.value
    if number == _sdk.TAG_DURATION_COMPACT and isinstance(value, (list, tuple)) and not value:
        # SDK 3.0 (#205): remove. SDK 2.0.0 indexes the compact form without checking its length and
        # raises IndexError on the server's zero duration (``[]``); 3.0.0b10 reads it.
        return _sdk.SdkDuration(0)
    if number == TAG_SET and isinstance(value, (list, tuple)):
        # SDK 3.0 (#205): remove. SDK 2.0.0 has no decoder for tag 56 (BufferError); 3.0.0b10 reads a
        # ``SurrealSet``: decide then what a read returns (a ``set`` today).
        return _set_of(value)
    if number in _GEOMETRY_TYPES:
        # SDK 3.0 (#205): keep while the SDK does not decode degenerate geometries (3.0.0b10 does not):
        # SDK 2.0.0's constructors refuse a line of fewer than 2 points (TypeError) and a ring of
        # fewer than 4 (InvalidGeometryError), and a polygon fails on a ring read as GeoJSON.
        try:
            return _geojson(tag)
        except TypeError:  # a malformed value, or a point: the SDK's error stands
            return _UNHANDLED
    return _UNHANDLED


def _set_of(items: list[Any] | tuple[Any, ...]) -> set[Any] | list[Any]:
    """A native set's items (already decoded by the SDK) as a ``set``, or a ``list`` when a ``set`` loses one.

    A ``list`` when an item is unhashable, and when two items are equal in Python (``1`` and
    ``True``, ``1`` and ``1.0``): the server already deduplicated the set, so a smaller ``set`` would
    merge distinct SurrealDB values and drop one without an error.
    """
    try:
        out = set(items)
    except TypeError:
        return list(items)
    return out if len(out) == len(items) else list(items)


def _geojson(tag: Any) -> dict[str, Any]:
    """The geometry *tag*, its members already decoded, as its GeoJSON ``dict``.

    ``TypeError`` for a ``Point``, which is never degenerate, and when the value does not hold
    members of the tag's kind, each a geometry the SDK built or one this hook read as GeoJSON.
    """
    kind = _GEOMETRY_TYPES[tag.tag]
    members = _members(tag.value)
    if kind == "GeometryCollection":
        return {"type": kind, "geometries": [_member_geojson(member) for member in members]}
    if kind not in _COMPOSITES:
        raise TypeError(f"a {kind} is never degenerate")
    _, _, member_kind = _COMPOSITES[kind]
    return {"type": kind, "coordinates": [_coordinates(member, member_kind) for member in members]}


def _members(value: Any) -> list[Any] | tuple[Any, ...]:
    """A geometry's members: an array, or ``TypeError``."""
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"a geometry's members are an array, not {type(value).__name__}")
    return value


def _member_geojson(member: Any) -> dict[str, Any]:
    """A collection member, an SDK geometry or one this hook read as GeoJSON, as its GeoJSON ``dict``."""
    if isinstance(member, dict):  # the hook reads a line, a polygon, a multi-geometry or a collection
        kind = member.get("type")
        if kind == "GeometryCollection" and "geometries" in member:
            return member
        if kind not in _COMPOSITES:
            raise TypeError(f"not a GeoJSON geometry this hook reads: {member!r}")
        _coordinates(member, kind)  # holds its coordinates, or TypeError
        return member
    if isinstance(member, _sdk.SdkGeometryCollection):
        return {"type": "GeometryCollection", "geometries": [_member_geojson(m) for m in _members(member.geometries)]}
    if isinstance(member, _sdk.SdkGeometryPoint):
        return {"type": "Point", "coordinates": _coordinates(member, "Point")}
    for kind, (cls, _, _) in _COMPOSITES.items():
        if isinstance(member, cls):
            return {"type": kind, "coordinates": _coordinates(member, kind)}
    raise TypeError(f"not a geometry: {type(member).__name__}")


def _coordinates(member: Any, kind: str) -> Any:
    """The GeoJSON coordinates, as plain lists, of *member*: a geometry of *kind*, or ``TypeError``.

    *member* is an SDK geometry, whose points the SDK built, or a GeoJSON ``dict`` of that kind this
    hook read: a position only ever comes from an SDK point, never from bare numbers.
    """
    if isinstance(member, dict):
        if member.get("type") != kind or "coordinates" not in member:
            raise TypeError(f"not a GeoJSON {kind}: {member!r}")
        return member["coordinates"]
    if kind == "Point":
        if not isinstance(member, _sdk.SdkGeometryPoint):
            raise TypeError(f"not a point: {type(member).__name__}")
        return [member.longitude, member.latitude]
    cls, attr, member_kind = _COMPOSITES[kind]
    if not isinstance(member, cls):
        raise TypeError(f"not a {kind}: {type(member).__name__}")
    return [_coordinates(m, member_kind) for m in _members(getattr(member, attr))]


# --- capability probes -----------------------------------------------------------------------------


def _sdk_round_trip(value: Any) -> Any:
    """*value* through the SDK's own CBOR codec, both read at call time; raises when either is missing."""
    encode = _sdk.cbor_encode
    decode = _sdk.cbor_decode
    if encode is None or decode is None:
        raise LookupError("the installed SDK has no CBOR codec in surrealdb.data.cbor")
    return decode(encode(value))


@functools.cache
def sdk_decodes_zero_duration() -> bool:
    """Whether the installed SDK reads a zero duration (computed once per process, no I/O).

    Round-trips ``CBORTag(TAG_DURATION_COMPACT, [])`` — the server's compact form of ``0ns`` — through
    the SDK's own codec and answers ``True`` only when that gives an SDK ``Duration`` equal to zero.
    ``False`` on any exception, and when the shim found no codec in the installed SDK.
    """
    try:
        value = _sdk_round_trip(_sdk.CBORTag(_sdk.TAG_DURATION_COMPACT, []))
        return bool(isinstance(value, _sdk.SdkDuration) and value.elapsed == 0)
    except Exception:  # any failure means "cannot read it"
        return False


@functools.cache
def sdk_decodes_native_sets() -> bool:
    """Whether the installed SDK reads a native set (CBOR tag 56) holding ``1`` and ``2``; same rules."""
    try:
        return bool(sorted(_sdk_round_trip(_sdk.CBORTag(TAG_SET, [1, 2]))) == [1, 2])
    except Exception:
        return False


@functools.cache
def sdk_decodes_degenerate_geometries() -> bool:
    """Whether the installed SDK reads a 1-point ``LineString`` and a 3-point ``Polygon`` ring; same rules."""
    point = _sdk.CBORTag(88, [0.0, 0.0])
    line = _sdk.CBORTag(89, [point])
    ring = _sdk.CBORTag(89, [point, _sdk.CBORTag(88, [1.0, 0.0]), point])
    try:
        _sdk_round_trip(line)
        _sdk_round_trip(_sdk.CBORTag(90, [ring]))
    except Exception:
        return False
    return True


def needed() -> bool:
    """Whether the installed SDK fails to decode at least one of the values the hook reads."""
    return not (sdk_decodes_zero_duration() and sdk_decodes_native_sets() and sdk_decodes_degenerate_geometries())


# --- installation ----------------------------------------------------------------------------------


def _with_global(function: Any, name: str, expected: Any, replacement: Any) -> types.FunctionType | None:
    """A copy of *function* — same code object — that reads the module global *name* as *replacement*.

    ``None`` unless *function* is a plain function whose code reads a global *name* that is
    *expected* in its module: the layout this module knows. The copy's globals are a snapshot of the
    module's, with that one name replaced; the module itself is never changed.
    """
    if not isinstance(function, types.FunctionType) or expected is None:
        return None
    if name not in function.__code__.co_names or function.__globals__.get(name) is not expected:
        return None
    namespace = dict(function.__globals__)
    namespace[name] = replacement
    clone = types.FunctionType(function.__code__, namespace, function.__name__, function.__defaults__, function.__closure__)
    clone.__kwdefaults__ = dict(function.__kwdefaults__) if function.__kwdefaults__ else None
    functools.update_wrapper(clone, function)
    return clone


@functools.cache
def _hooked_decode() -> Callable[[bytes], Any] | None:
    """The SDK's ``decode``, run with ``read_tag_hook`` as its ``tag_decoder``; ``None`` for another layout."""
    # SDK 3.0 (#205): SDK 2.0.0's ``decode(data)`` is ``loads(data, tag_hook=tag_decoder)`` with a
    # module-level ``tag_decoder``. Re-check against the stable SDK's codec.
    return _with_global(_sdk.cbor_decode, "tag_decoder", _sdk.cbor_tag_decoder, read_tag_hook)


@functools.cache
def _hooked_methods(cls: type[Any]) -> tuple[tuple[str, types.FunctionType], ...]:
    """*cls*'s methods that decode a response, each as a copy whose ``decode`` is the hooked one."""
    decode = _hooked_decode()
    if decode is None:
        return ()
    methods: list[tuple[str, types.FunctionType]] = []
    for name in _DECODING_METHODS:
        clone = _with_global(getattr(cls, name, None), "decode", _sdk.cbor_decode, decode)
        if clone is not None:
            methods.append((name, clone))
    return tuple(methods)


def available() -> bool:
    """Whether the installed SDK has the layout the hook knows, for its three async connection kinds."""
    kinds = (_sdk.SdkAsyncWsConnection, _sdk.SdkAsyncHttpConnection, _sdk.SdkAsyncEmbeddedConnection)
    return all(cls is not None and bool(_hooked_methods(cls)) for cls in kinds)


def active() -> bool:
    """Whether the clients the ORM opens get the hook: the SDK needs it and has the known layout."""
    return needed() and available()


def install(client: Any) -> bool:
    """Make *client* — one the ORM has just built, not yet connected — decode responses with the hook.

    Sets the client's decoding method(s) on the instance (see the module docstring). ``True`` when
    installed; ``False``, leaving *client* as it is, when the hook is not ``active()`` or *client*'s
    class has no method this module knows.
    """
    if not active():
        return False
    methods = _hooked_methods(client.__class__)
    for name, function in methods:
        setattr(client, name, types.MethodType(function, client))
    return bool(methods)
