"""Versioned storage (v0.26.0): the values ``QuerySet.as_of()`` takes, the SurrealQL it compiles to, its errors.

Internal: not part of the public API (``as_of`` is a ``QuerySet`` method; nothing here is re-exported).

A time-travel read is SurrealQL's ``SELECT … VERSION $_as_of``, one statement per ``client.query()`` call
(the SDK returns the first statement's result and drops the others' errors), with the moment always a
**bound** ``datetime``. Every table read also carries a per-record check, :data:`GUARD`::

    WHERE <filters> AND (SELECT VALUE id FROM ONLY $parent.id VERSION $_as_of) != NONE

because a plain ``VERSION`` table read is wrong on both server lines: SurrealDB 3.x returns records
deleted before the moment, in their last state (so filtered counts, sums and groups are wrong too), and
SurrealDB 2.x miscounts an unfiltered ``count() … GROUP ALL``. Reading each scanned record by its id at
the moment is right on every versioned store (3.x, 2.x and the SDK's embedded ``surrealkv+versioned://``
engine), with ``ORDER BY``/``LIMIT``/``START``/``FETCH``, ``GROUP ALL`` and ``GROUP BY``; it costs about one
extra point read per record scanned. A read by record id needs no check.

:func:`normalise_as_of` turns the value given to ``as_of()`` into an :class:`AsOf` once, at the call: an
absolute moment (a ``datetime`` or an ISO-8601 string, naive meaning UTC) is converted to UTC and checked
against :data:`MIN_MOMENT`/:data:`MAX_MOMENT` there; "how long ago" (a ``timedelta`` or a ``Duration``)
and a ``SurrealFunc`` expression are resolved by :func:`resolve`, once per read, on the **database's**
clock (``RETURN time::now() - $_as_of_ago;`` / ``RETURN <expression>;``), then checked against the same
bounds before the read is sent.

The texts of the errors the ORM raises for time travel are built here, so each lives in one place (the
embedded engine's own refusal is ``embedded.history_unsupported()``).

A leaf module: it imports ``_sdk``, ``encoding``, ``exceptions``, ``functions``, ``types`` and the standard
library only.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from . import encoding as _encoding
from ._sdk import SdkDuration, ServerError
from .exceptions import SurrealDbError
from .functions import SurrealFunc
from .types import Duration

#: The earliest and the latest moment a version can address. SurrealDB stores a version as nanoseconds
#: since 1970-01-01T00:00:00Z in a signed 64-bit integer (floored here to Python's microseconds). Both are
#: enforced by the ORM: SurrealDB 2.x reads an earlier moment as the *current* state, without an error,
#: and every line fails after the later one.
MIN_MOMENT: Final = datetime(1970, 1, 1, tzinfo=UTC)
MAX_MOMENT: Final = datetime(2262, 4, 11, 23, 47, 16, 854775, tzinfo=UTC)

#: The per-record check of every time-travel table read (see the module docstring).
GUARD: Final = "(SELECT VALUE id FROM ONLY $parent.id VERSION $_as_of) != NONE"
#: Appended right before the final ``;`` of a time-travel read.
VERSION_CLAUSE: Final = " VERSION $_as_of"
#: The bound variable holding the moment (a reserved ``variables()`` name: the moment overrides it).
MOMENT_VARIABLE: Final = "_as_of"
#: The bound variable holding "how long ago" while it is resolved.
AGO_VARIABLE: Final = "_as_of_ago"
#: The text of the server's refusal when its storage keeps no history (3.x and 2.x RocksDB alike).
UNSUPPORTED_MARKER: Final = "does not support versioned queries"

_RESOLVE_AGO: Final = f"RETURN time::now() - ${AGO_VARIABLE};"
_VERSION: Final = re.compile(r"(\d+)\.\d+")


@dataclass(frozen=True, slots=True)
class AsOf:
    """The moment of an ``as_of()`` read, normalised: exactly one of ``moment``, ``ago``, ``expression`` is set.

    ``moment`` is an aware UTC ``datetime`` within :data:`MIN_MOMENT` … :data:`MAX_MOMENT`; ``ago`` a
    ``timedelta`` or an ORM ``Duration`` (an SDK ``Duration`` is converted) to subtract from the database's
    clock; ``expression`` a ``SurrealFunc`` the database evaluates to a datetime. ``given`` keeps the value
    passed to ``as_of()``, which the resolution errors quote.
    """

    given: Any
    moment: datetime | None = None
    ago: timedelta | Duration | None = None
    expression: SurrealFunc | None = None


def in_range(moment: datetime) -> bool:
    """Whether the aware *moment* lies within :data:`MIN_MOMENT` … :data:`MAX_MOMENT`, bounds included."""
    return MIN_MOMENT <= moment <= MAX_MOMENT


def _utc(moment: datetime) -> datetime:
    """*moment* in UTC, a naive one read as UTC.

    A moment with no UTC form in Python (``astimezone`` overflows near ``datetime.min``/``datetime.max``)
    comes back aware but unconverted: it lies outside the bounds anyway, and the error shows it as given.
    """
    if moment.utcoffset() is None:
        return moment.replace(tzinfo=UTC)
    try:
        return moment.astimezone(UTC)
    except OverflowError:
        return moment


def _absolute(at: Any, moment: datetime) -> AsOf:
    """An absolute moment given to ``as_of()``, in UTC; ``ValueError`` outside the bounds."""
    utc = _utc(moment)
    if not in_range(utc):
        raise ValueError(moment_out_of_range(utc))
    return AsOf(given=at, moment=utc)


def _refused_duration(at: Any) -> ValueError:
    """The refusal of a negative duration, or of an SDK ``Duration`` the ORM ``Duration`` refuses."""
    return ValueError(f"as_of() takes how long ago as a duration >= 0 in whole nanoseconds, got {at!r}")


def normalise_as_of(at: object) -> AsOf | None:
    """Normalise the value given to ``QuerySet.as_of()``; ``None`` clears the moment.

    :raises TypeError: none of the accepted forms — a ``date`` that is not a ``datetime``, a number, a
        ``bool``, ``bytes``….
    :raises ValueError: a string ``datetime.fromisoformat`` refuses; a negative ``timedelta``, or an SDK
        ``Duration`` whose ``elapsed`` is not an ``int`` >= 0 within ``Duration.MAX_NANOSECONDS``; an absolute
        moment outside :data:`MIN_MOMENT` … :data:`MAX_MOMENT`, compared in UTC.
    """
    if at is None:
        return None
    if isinstance(at, datetime):
        return _absolute(at, at)
    if isinstance(at, str):
        try:
            parsed = datetime.fromisoformat(at)
        except ValueError:
            raise ValueError(f"as_of() {at!r} is not an ISO-8601 date-time") from None
        return _absolute(at, parsed)
    if isinstance(at, timedelta):
        if at < timedelta(0):
            raise _refused_duration(at)
        return AsOf(given=at, ago=at)
    if isinstance(at, Duration):
        return AsOf(given=at, ago=at)
    if isinstance(at, SdkDuration):  # not a ``Duration`` subclass, nor the other way round
        try:
            ago = Duration(at.elapsed)
        except (TypeError, ValueError):
            raise _refused_duration(at) from None
        return AsOf(given=at, ago=ago)
    if isinstance(at, SurrealFunc):
        return AsOf(given=at, expression=at)
    raise TypeError(
        "as_of() takes a datetime, an ISO-8601 string, a timedelta or Duration (how long ago), a SurrealFunc or "
        f"None, not {type(at).__name__}"
    )


async def resolve(as_of: AsOf, client: Any, variables: Mapping[str, Any]) -> datetime:
    """The aware UTC moment of a read, resolved by the database when it is a duration or an expression.

    One ``RETURN`` request for ``ago`` (``time::now() - $_as_of_ago``, bound) or ``expression`` (inlined,
    with the queryset's ``variables`` bound); none for an absolute ``moment``, already checked.

    :raises SurrealDbError: the database could not evaluate it (chained to the SDK's ``ServerError`` or the
        embedded engine's ``RuntimeError``), it did not give a ``datetime``, or it gave a moment outside
        :data:`MIN_MOMENT` … :data:`MAX_MOMENT` — the read is never sent then.
    """
    if as_of.moment is not None:
        return as_of.moment
    spec = repr(as_of.given)
    if as_of.ago is not None:
        statement, bound = _RESOLVE_AGO, {AGO_VARIABLE: as_of.ago}
    elif as_of.expression is not None:
        statement, bound = f"RETURN {as_of.expression.expression};", dict(variables)
    else:  # pragma: no cover - normalise_as_of always sets one of the three
        raise RuntimeError("internal: an as_of() moment with nothing to resolve")
    try:
        value = await client.query(statement, _encoding.encode(bound))
    except (ServerError, RuntimeError) as exc:
        raise SurrealDbError(evaluation_failed(spec, exc)) from exc
    if not isinstance(value, datetime):
        raise SurrealDbError(not_a_datetime(spec, value))
    moment = _utc(value)
    if not in_range(moment):
        raise SurrealDbError(resolved_out_of_range(spec, moment))
    return moment


def line_major(version: object) -> int:
    """The major of a ``client.version()`` answer (``"surrealdb-3.3.4"`` → 3); 3 when none can be read."""
    match = _VERSION.search(str(version))
    return int(match.group(1)) if match else 3


# --- message builders (one place per text) --------------------------------------------------------------


def transaction_refused() -> str:
    """The refusal of ``as_of()`` on a queryset bound to a transaction (``objects(tx=)``)."""
    return (
        "as_of() reads the database's history, which a transaction cannot join: run this read on "
        "Model.objects(), outside objects(tx=)."
    )


def queryset_refused(method: str) -> str:
    """The refusal of a write or stream (*method*, with its ``()``) on a queryset read ``as_of()``."""
    return (
        f"{method} is not available on a queryset read as_of(): a time-travel read is read-only and cannot be "
        "streamed. Call it on a queryset without as_of()."
    )


def server_refused(exc: BaseException) -> str:
    """The refusal when the server's storage keeps no history (*exc* is its ``ServerError``)."""
    return (
        f"as_of() needs a store that keeps history, and this SurrealDB server's storage does not ({exc}). Start "
        "SurrealDB 3.x with surrealkv://<path>?versioned=true or rocksdb://<path>?versioned=true, SurrealDB 2.x "
        "with surrealkv+versioned://<path>."
    )


def fetch_refused() -> str:
    """The refusal of ``fetch()`` under ``as_of()`` on SurrealDB 2.x, for a read that returns the fetched records."""
    return (
        "fetch() under as_of() needs SurrealDB 3.x: SurrealDB 2.x (servers and the embedded engine of the "
        "surrealdb SDK 2.x) resolves fetched links to their current state, not as they were at that moment. "
        "Drop fetch() and read the linked records with their own as_of() query."
    )


def snapshot_write_refused(model: str, method: str, moment: datetime) -> str:
    """The refusal of a write (*method*, with its ``()``) on an instance of *model* read at *moment* with
    ``as_of()``."""
    return (
        f"{model}.{method} cannot run on an instance read with as_of(): it holds the record as it was at "
        f"{moment.isoformat()}, not the current record. To restore that state, build a new instance from it "
        f"({model}(**snapshot.model_dump())) and save() or update() it; refresh() turns the snapshot back into "
        "the current record."
    )


def not_a_datetime(spec: str, value: object) -> str:
    """The error when the duration or expression *spec* (the ``repr`` given to ``as_of()``) did not give a datetime."""
    return f"as_of({spec}) must give a datetime; the database returned {value!r}"


def evaluation_failed(spec: str, exc: BaseException) -> str:
    """The error when the database could not evaluate the duration or expression *spec*."""
    return f"as_of({spec}) could not be evaluated by the database: {exc}"


def resolved_out_of_range(spec: str, value: datetime) -> str:
    """The error when the duration or expression *spec* gave *value*, outside the moments a version can address."""
    return (
        f"as_of({spec}) gave {value.isoformat()}, outside the moments SurrealDB versions can address "
        f"({MIN_MOMENT.isoformat()} to {MAX_MOMENT.isoformat()}): SurrealDB 2.x would read an earlier moment as "
        "the current state."
    )


def moment_out_of_range(moment: datetime) -> str:
    """The refusal of an absolute moment (in UTC) outside the moments a version can address."""
    return (
        f"as_of() takes a moment from {MIN_MOMENT.isoformat()} to {MAX_MOMENT.isoformat()}, the instants "
        "SurrealDB versions can address (SurrealDB 2.x reads an earlier moment as the current state), got "
        f"{moment.isoformat()}"
    )
