"""Internal: the read side of the SDK boundary (#235), the counterpart of ``encoding.py``.

Not part of the public API — nothing here is exported by ``surreal_orm_lite``.

The surrealdb SDK decodes every response with its strict CBOR decoder (``surrealdb.data._decoder.Decoder``), which
refuses two kinds of values SurrealDB stores and returns, with ``UnexpectedResponseError``:

* a ``LineString`` of fewer than 2 points and a ``Polygon`` ring of fewer than 4 points once closed, alone or inside
  another geometry (CBOR tags 89-94);
* a datetime before ``0001-01-01T00:00:00Z`` or after ``9999-12-31T23:59:59.999999999Z`` (tag 12, the years a Python
  ``datetime`` holds), which SurrealDB stores from year -262143 to +262142 (#279).

The clients the ORM opens read them as in 0.40.x: a degenerate geometry is its GeoJSON ``dict`` (``{"type": …,
"coordinates": …}``, or ``"geometries"`` for a collection), which a geometry field validates or refuses clearly, and an
out-of-range datetime is its RFC 3339 text as SurrealDB writes it (``"0000-06-01T00:00:00Z"``,
``"+10000-01-01T00:00:00.500Z"``), which a ``datetime`` field refuses clearly. Every other value is the SDK's own: a
valid geometry is the SDK's geometry, a datetime within years 1-9999 the SDK's ``PreciseDatetime``, a zero duration
and a native set what the SDK reads. Anything else the SDK refuses stays refused with the SDK's error, and an
undecodable value raises ``SurrealDbConnectionError`` through the ORM (``_client.py``).

``decode(data, *, v2=False)`` is the SDK's ``surrealdb.data.cbor.decode`` (same signature, same default) run with
``_OrmDecoder``, a subclass of the SDK's ``Decoder`` whose ``_tag()`` reads tags 12 and 88-94 itself: it reads the
payload (nested tags go through this same ``_tag()`` first), hands it to the SDK's own reader for that tag
(``surrealdb.data._decoder._TAGS``) and, only when that reader refuses it, reads the value as above. Every other tag
goes to the SDK's ``_tag()``.

``install(client)`` gives ``decode`` to one client, the one ``ConnectionManager._open_client`` has just built. The SDK
binds ``decode`` at module level and calls it in a few methods (``_route_frame`` over WebSocket and embedded,
``decode_response`` and ``check_status_for_error`` over HTTP, ``_send`` on the embedded engine). Each is set on the
instance as a copy of the SDK's own function — the same code object — whose module globals are a copy of its module's
with ``decode`` replaced. Nothing is patched, forked or vendored: the code that runs is the SDK's, its modules and
classes are untouched, and a client the application builds itself with ``AsyncSurreal`` keeps the SDK's behaviour.

The hook is installed only when the installed SDK cannot decode one of these values (``needed()``: capability probes
on hand-built CBOR, run once, no I/O, never a version comparison) and only when the SDK's layout is the one described
above (``available()``).
"""

from __future__ import annotations

import functools
import inspect
import types
from typing import Any, Final

from . import _sdk

__all__ = [
    "active",
    "available",
    "decode",
    "install",
    "needed",
    "sdk_decodes_degenerate_geometries",
    "sdk_decodes_out_of_range_datetimes",
]

#: The GeoJSON ``type`` of each geometry tag of the SurrealDB protocol.
_GEOMETRY_TYPES: Final[dict[int, str]] = {
    _sdk.TAG_GEOMETRY_POINT: "Point",
    _sdk.TAG_GEOMETRY_LINE: "LineString",
    _sdk.TAG_GEOMETRY_POLYGON: "Polygon",
    _sdk.TAG_GEOMETRY_MULTI_POINT: "MultiPoint",
    _sdk.TAG_GEOMETRY_MULTI_LINE: "MultiLineString",
    _sdk.TAG_GEOMETRY_MULTI_POLYGON: "MultiPolygon",
    _sdk.TAG_GEOMETRY_COLLECTION: "GeometryCollection",
}

#: The tags ``_OrmDecoder._tag()`` reads itself: the compact datetime and the seven geometries.
_OWN_TAGS: Final[frozenset[int]] = frozenset({_sdk.TAG_DATETIME_COMPACT, *_GEOMETRY_TYPES})

#: The geometries made of members, by GeoJSON ``type``: the SDK class, the attribute listing its
#: members and the members' GeoJSON ``type``. A ``GeometryCollection`` holds members of any type.
_COMPOSITES: Final[dict[str, tuple[type[Any], str, str]]] = {
    "LineString": (_sdk.SdkGeometryLine, "geometry_points", "Point"),
    "Polygon": (_sdk.SdkGeometryPolygon, "geometry_lines", "LineString"),
    "MultiPoint": (_sdk.SdkGeometryMultiPoint, "geometry_points", "Point"),
    "MultiLineString": (_sdk.SdkGeometryMultiLine, "geometry_lines", "LineString"),
    "MultiPolygon": (_sdk.SdkGeometryMultiPolygon, "geometry_polygons", "Polygon"),
}

#: The SDK methods that decode a response with the module-global ``decode``, by name: ``_route_frame`` (WebSocket and
#: embedded), ``decode_response`` and ``check_status_for_error`` (static, HTTP), ``_send`` (embedded).
#: ``tests/test_decoding.py::TestSdkLayout`` pins them; a client's method that does not read the SDK's ``decode`` as a
#: module global is left alone.
_DECODING_METHODS: Final = ("_route_frame", "decode_response", "check_status_for_error", "_send")

_UNHANDLED: Final = object()

_NS_PER_SECOND: Final = 1_000_000_000
_SECONDS_PER_DAY: Final = 86_400
#: The first and the last second a Python ``datetime`` holds: 0001-01-01T00:00:00Z and
#: 9999-12-31T23:59:59Z, in seconds since 1970-01-01T00:00:00Z.
_FIRST_SECOND: Final = -62_135_596_800
_LAST_SECOND: Final = 253_402_300_799
#: Days from 0000-03-01 to 1970-01-01, and in a 400-year cycle of the proleptic Gregorian calendar.
_DAYS_TO_EPOCH: Final = 719_468
_DAYS_PER_ERA: Final = 146_097


# --- the decoder ---------------------------------------------------------------------------------

#: The SDK's decoder class, or ``object`` when the installed SDK has none (``available()`` is then false and
#: ``decode`` is never installed).
_DecoderBase: Any = _sdk.SdkDecoder if _sdk.SdkDecoder is not None else object


class _OrmDecoder(_DecoderBase):
    """The SDK's ``Decoder``, except that degenerate geometries and out-of-range datetimes are read as in 0.40.x."""

    def _tag(self, tag: int) -> Any:
        """Tag *tag*'s value: the SDK's for every tag but 12 and 88-94, which are read here (module docstring)."""
        if tag not in _OWN_TAGS:
            return super()._tag(tag)
        raw = self._value()  # nested tags went through this ``_tag()`` first
        try:
            return _sdk.SDK_TAG_READERS[tag](raw)
        except _sdk.UnexpectedResponseError:
            value = _fallback(tag, raw)
            if value is _UNHANDLED:
                raise
            return value


def decode(data: bytes, *, v2: bool = False) -> Any:
    """The SDK's ``surrealdb.data.cbor.decode``, reading degenerate geometries and out-of-range datetimes as in 0.40.x.

    Same signature and default as the SDK's (``v2``: the payload is ``cbor.2``). Raises the SDK's
    ``UnexpectedResponseError`` for anything else the SDK refuses.
    """
    return _OrmDecoder(bytes(data), v2=v2).decode()


def _fallback(tag: int, raw: Any) -> Any:
    """What the decoder reads for tag *tag* over *raw* when the SDK's reader refused it, or ``_UNHANDLED``."""
    if tag in _GEOMETRY_TYPES:
        # SDK 3.0 (#205): kept. SurrealDB stores and returns a line of fewer than 2 points and a ring of
        # fewer than 4, which the SDK's geometry readers refuse; such a geometry, or one holding it, is read
        # as its GeoJSON, as in 0.40.x.
        try:
            return _geojson(tag, raw)
        except TypeError:  # a malformed value, or a point: the SDK's error stands
            return _UNHANDLED
    if _is_compact_datetime(raw):
        # SDK 3.0 (#205): kept. SurrealDB stores datetimes from year -262143 to +262142, the SDK reads
        # tag 12 into a Python ``datetime``, which holds years 1-9999 only; outside them the value is read
        # as its text, as in 0.40.x.
        seconds, nanoseconds = raw
        if not _FIRST_SECOND <= seconds <= _LAST_SECOND:
            return _datetime_text(seconds, nanoseconds)
    return _UNHANDLED


def _is_compact_datetime(value: Any) -> bool:
    """Whether *value* is the server's compact datetime: ``[seconds, nanoseconds]``, two integers, the
    nanoseconds within a second."""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return False
    if not all(isinstance(part, int) and not isinstance(part, bool) for part in value):
        return False
    return bool(0 <= value[1] < _NS_PER_SECOND)


def _datetime_text(seconds: int, nanoseconds: int) -> str:
    """The RFC 3339 text SurrealDB writes for the UTC datetime ``seconds`` + ``nanoseconds`` after 1970.

    Integer arithmetic on the proleptic Gregorian calendar, year 0 included (a Python ``datetime`` holds
    years 1-9999 only). The server's own form (``<string>``): a year within 0000-9999 has four digits,
    any other a sign and at least four (``-0001``, ``+10000``); the fraction has 3, 6 or 9 digits, the
    fewest that keep every nanosecond, and is left out when zero.
    """
    days, second_of_day = divmod(seconds, _SECONDS_PER_DAY)
    year, month, day = _civil_date(days)
    hour, rest = divmod(second_of_day, 3600)
    minute, second = divmod(rest, 60)
    year_text = f"{year:04d}" if 0 <= year <= 9999 else f"{year:+05d}"
    if nanoseconds == 0:
        fraction = ""
    elif nanoseconds % 1_000_000 == 0:
        fraction = f".{nanoseconds // 1_000_000:03d}"
    elif nanoseconds % 1_000 == 0:
        fraction = f".{nanoseconds // 1_000:06d}"
    else:
        fraction = f".{nanoseconds:09d}"
    return f"{year_text}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}{fraction}Z"


def _civil_date(days: int) -> tuple[int, int, int]:
    """The proleptic Gregorian ``(year, month, day)`` *days* after 1970-01-01, for any integer.

    Howard Hinnant's ``civil_from_days``: the year starts on March 1st, so the leap day ends it, and
    a 400-year cycle (an era) always holds 146097 days. Python's floor division keeps it exact before
    year 0.
    """
    shifted = days + _DAYS_TO_EPOCH
    era, day_of_era = divmod(shifted, _DAYS_PER_ERA)  # day_of_era: 0 … 146096
    year_of_era = (day_of_era - day_of_era // 1460 + day_of_era // 36524 - day_of_era // 146096) // 365  # 0 … 399
    day_of_year = day_of_era - (365 * year_of_era + year_of_era // 4 - year_of_era // 100)  # 0 … 365, from March 1st
    shifted_month = (5 * day_of_year + 2) // 153  # 0 … 11, March first
    day = day_of_year - (153 * shifted_month + 2) // 5 + 1
    month = shifted_month + 3 if shifted_month < 10 else shifted_month - 9
    year = year_of_era + era * 400 + (1 if month <= 2 else 0)
    return year, month, day


def _geojson(tag: int, value: Any) -> dict[str, Any]:
    """The geometry of tag *tag* over *value*, its members already decoded, as its GeoJSON ``dict``.

    ``TypeError`` for a ``Point``, which is never degenerate, and when the value does not hold
    members of the tag's kind, each a geometry the SDK built or one this hook read as GeoJSON.
    """
    kind = _GEOMETRY_TYPES[tag]
    members = _members(value)
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
    sdk_decode = _sdk.cbor_decode
    if encode is None or sdk_decode is None:
        raise LookupError("the installed SDK has no CBOR codec in surrealdb.data.cbor")
    return sdk_decode(encode(value))


@functools.cache
def sdk_decodes_degenerate_geometries() -> bool:
    """Whether the installed SDK reads a 1-point ``LineString`` and a 3-point ``Polygon`` ring (once, no I/O).

    ``False`` on any exception, and when ``_sdk.py`` found no codec in the installed SDK.
    """
    point = _sdk.CBORTag(_sdk.TAG_GEOMETRY_POINT, [0.0, 0.0])
    line = _sdk.CBORTag(_sdk.TAG_GEOMETRY_LINE, [point])
    ring = _sdk.CBORTag(_sdk.TAG_GEOMETRY_LINE, [point, _sdk.CBORTag(_sdk.TAG_GEOMETRY_POINT, [1.0, 0.0]), point])
    try:
        _sdk_round_trip(line)
        _sdk_round_trip(_sdk.CBORTag(_sdk.TAG_GEOMETRY_POLYGON, [ring]))
    except Exception:  # any failure means "cannot read it"
        return False
    return True


@functools.cache
def sdk_decodes_out_of_range_datetimes() -> bool:
    """Whether the installed SDK reads a datetime before year 1 and one after year 9999; same rules.

    Round-trips ``0000-12-31T23:59:59Z`` and ``+10000-01-01T00:00:00Z`` (CBOR tag 12, the server's
    ``[seconds, nanoseconds]``), the two seconds just outside a Python ``datetime``, and answers ``True``
    only when the SDK decodes both.
    """
    try:
        _sdk_round_trip(_sdk.CBORTag(_sdk.TAG_DATETIME_COMPACT, [_FIRST_SECOND - 1, 0]))
        _sdk_round_trip(_sdk.CBORTag(_sdk.TAG_DATETIME_COMPACT, [_LAST_SECOND + 1, 0]))
    except Exception:
        return False
    return True


def needed() -> bool:
    """Whether the installed SDK fails to decode at least one of the values the hook reads."""
    return not (sdk_decodes_degenerate_geometries() and sdk_decodes_out_of_range_datetimes())


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
def _hooked_methods(cls: type[Any]) -> tuple[tuple[str, types.FunctionType, bool], ...]:
    """*cls*'s methods that decode a response, each as ``(name, copy reading decode(), is a staticmethod)``."""
    methods: list[tuple[str, types.FunctionType, bool]] = []
    for name in _DECODING_METHODS:
        member = inspect.getattr_static(cls, name, None)
        static = isinstance(member, staticmethod)
        clone = _with_global(member.__func__ if static else member, "decode", _sdk.cbor_decode, decode)
        if clone is not None:
            methods.append((name, clone, static))
    return tuple(methods)


def _decoder_layout() -> bool:
    """Whether the SDK's decoder is the one ``_OrmDecoder`` extends: ``_tag``/``_value``, a reader per own tag."""
    decoder, readers = _sdk.SdkDecoder, _sdk.SDK_TAG_READERS
    if not isinstance(decoder, type) or not isinstance(readers, dict):
        return False
    if not all(callable(getattr(decoder, name, None)) for name in ("_tag", "_value", "decode")):
        return False
    return all(callable(readers.get(tag)) for tag in _OWN_TAGS)


def available() -> bool:
    """Whether the installed SDK has the layout the hook knows: its decoder, and the WebSocket and HTTP clients'
    decoding methods (the embedded client's too when the ``embedded`` extra is installed)."""
    if not _decoder_layout():
        return False
    kinds = (_sdk.SdkAsyncWsConnection, _sdk.SdkAsyncHttpConnection)
    if not all(cls is not None and bool(_hooked_methods(cls)) for cls in kinds):
        return False
    embedded = _sdk.SdkAsyncEmbeddedConnection
    return embedded is None or bool(_hooked_methods(embedded))


def active() -> bool:
    """Whether the clients the ORM opens get the hook: the SDK needs it and has the known layout."""
    return needed() and available()


def install(client: Any) -> bool:
    """Make *client* — one the ORM has just built, not yet connected — decode responses with ``decode``.

    Sets the client's decoding methods on the instance (see the module docstring): a static method as the plain copy,
    any other bound to *client*. ``True`` when installed; ``False``, leaving *client* as it is, when the hook is not
    ``active()`` or *client*'s class has no method this module knows.
    """
    if not active():
        return False
    methods = _hooked_methods(client.__class__)
    for name, function, static in methods:
        setattr(client, name, function if static else types.MethodType(function, client))
    return bool(methods)
