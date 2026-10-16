"""The SDK's in-process SurrealDB engine (v0.25.0): which URLs address it, and what it cannot do.

The official SDK routes a URL with its own parser (``surrealdb.connections.url``): ``AsyncSurreal(url)``
opens a server transport for ``http``, ``https``, ``ws`` and ``wss``, and its in-process engine
(``AsyncEmbeddedSurrealConnection``) for every other scheme the parser recognises — ``mem``, ``memory``,
``file``, ``surrealkv`` and ``surrealkv+versioned`` with SDK 2.0.0. This module asks that same parser
(through ``_sdk``) and keeps **no list of embedded schemes**, so a scheme a later SDK release adds
(#233) needs no change here. A URL the parser refuses gets one clear ORM error quoting the
SDK's own message (:func:`require_supported_url`) before anything is opened.

The texts of the errors the ORM raises for what the engine cannot do (live queries, a second session,
model auth on a session of its own, ``changes(since=<datetime>)``) are built here too, so each lives in
one place.

A leaf module: it imports ``_sdk``, ``exceptions`` and the standard library only.
"""

import contextlib
import functools
import urllib.parse
from typing import Final, Literal

from ._sdk import SdkUrl, SdkUrlScheme
from .exceptions import SurrealDbConnectionError

#: How the official SDK treats a URL: a server transport, its in-process engine, or a refusal.
UrlKind = Literal["server", "embedded", "refused"]

#: The SDK's server transports. Every other scheme its parser recognises addresses the in-process engine.
_NETWORK_SCHEMES: Final = frozenset({"http", "https", "ws", "wss"})


@functools.lru_cache(maxsize=64)
def url_kind(url: str) -> UrlKind:
    """How the official SDK treats *url*: a server transport, its in-process engine, or a refusal.

    Classified by the SDK's own parser (``SdkUrl``): ``"refused"`` when it raises (any ``Exception``: 2.0.0
    raises ``ValueError``, the 3.0 line ``UnsupportedEngineError``/``InvalidUrlError``); ``"server"`` for
    http, https, ws and wss; ``"embedded"`` for every other scheme it recognises — on SDK 2.0.0 ``mem``,
    ``memory``, ``file``, ``surrealkv``, ``surrealkv+versioned`` — so a scheme a later SDK adds (#233)
    needs no change here.
    """
    try:
        scheme = SdkUrl(url).scheme
    except Exception:
        return "refused"
    if isinstance(scheme, SdkUrlScheme) and scheme.value not in _NETWORK_SCHEMES:
        return "embedded"
    return "server"


def is_embedded_url(url: str | None) -> bool:
    """Whether the official SDK routes *url* to its in-process engine. False for ``None``/``""``."""
    if not url:  # an explicit test: mypy does not narrow ``str | None`` through ``bool(url) and …``
        return False
    return url_kind(url) == "embedded"


def require_supported_url(url: str | None) -> None:
    """Raise E11 when the SDK's parser refuses *url*; nothing for ``None``/``""`` or an accepted URL.

    Parses again on the refusal path only (``url_kind`` is cached and keeps no exception), so the error is
    chained to the SDK's own exception.

    :raises SurrealDbConnectionError: quoting the SDK's message and the schemes it accepts (naming #233
        for ``rocksdb://``), with ``retryable = False``: the same URL can never succeed.
    """
    if not url or url_kind(url) != "refused":
        return
    try:
        SdkUrl(url)
    except Exception as exc:
        error = SurrealDbConnectionError(unsupported_url_message(url, exc))
        error.retryable = False  # type: ignore[attr-defined]
        raise error from exc


def unsupported_url_message(url: str, exc: BaseException) -> str:
    """E11: the installed SDK cannot open *url* (``exc`` is its parser's exception)."""
    schemes = ", ".join(sorted(s.value for s in SdkUrlScheme))
    hint = ""
    with contextlib.suppress(ValueError):  # a URL ``urlsplit`` cannot split gets no hint
        if urllib.parse.urlsplit(url).scheme.lower() == "rocksdb":
            hint = (
                " Embedded RocksDB (rocksdb://) is not supported by the official SDK yet: "
                "see https://github.com/EulogySnowfall/SurrealDB-ORM-lite/issues/233."
            )
    return f"The installed surrealdb SDK cannot open {url!r}: {exc}. The URL schemes it accepts are: {schemes}.{hint}"


def live_unsupported(url: str | None) -> str:
    """E1: a live query (``live()``, ``watch()``, ``subscribe_live()``) on the embedded engine."""
    return (
        f"Live queries need a WebSocket server connection (ws:// or wss://): the embedded engine ({url!r}) has no "
        "notification channel to deliver them. Read the table's change feed with Model.objects().changes() "
        "instead, or connect to a server."
    )


def ephemeral_unsupported(url: str | None) -> str:
    """E2: ``SurrealDBConnectionManager.ephemeral_client()`` on the embedded engine."""
    return (
        f"ephemeral_client() opens a second session on the configured database, which the embedded engine "
        f"({url!r}) cannot do: every in-process client is a database of its own. Use the shared client "
        "(get_client()), or connect to a server."
    )


def model_auth_unsupported(model: str, label: str, url: str | None) -> str:
    """E3: model-level auth (``signup``/``signin``/``authenticate``/``refresh``) without ``bind=True``."""
    return (
        f"{model}.{label}() runs on a session of its own so that the shared connection keeps its identity, and "
        f"the embedded engine ({url!r}) has a single session per database. Pass bind=True to sign that session "
        "in (every model then runs as this user, in every event loop of the process), or connect to a server."
    )


def changes_since_datetime_unsupported() -> str:
    """E4: ``changes(since=<datetime>)`` on the embedded engine of the surrealdb SDK 2.x."""
    return (
        "changes(since=<datetime>) is not available on the embedded engine of the surrealdb SDK 2.x: it keeps "
        "no timestamps for change-feed positions, so a datetime cannot be resolved. Pass a cursor "
        "(ChangeModelStream.cursor) or since=None."
    )
