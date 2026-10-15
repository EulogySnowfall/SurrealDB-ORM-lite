"""The official SDK's in-process SurrealDB engine (v0.25.0): ``mem://``, ``surrealkv://<path>`` and the
other schemes the SDK routes to it.

Unit tests first (URL classification, error texts: no engine, no server); the classes that open an engine
or a server are suffixed ``E2E``. Most E2E classes need no server at all: the engine runs in-process.
"""

import ast
import enum
import inspect
import sys
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlparse

import pytest
import surrealdb

from surreal_orm_lite import SurrealDBConnectionManager, _sdk, connection_manager, embedded
from surreal_orm_lite.embedded import is_embedded_url, require_supported_url, url_kind
from surreal_orm_lite.exceptions import SurrealDbConnectionError
from tests.conftest import _ws_url


def _embedded_engine_available() -> bool:
    """Whether the installed SDK ships its in-process engine.

    Always true with SDK 2.0.0 (the engine is in its base wheel); SDK 3.0 moves it to the optional
    ``surrealdb[embedded]`` extra (#205), and without it ``AsyncSurreal("mem://")`` raises.
    """
    try:
        surrealdb.AsyncSurreal("mem://")
    except Exception:
        return False
    return True


pytestmark = pytest.mark.skipif(not _embedded_engine_available(), reason="the installed surrealdb SDK has no embedded engine")

#: The URL schemes of the SDK's own parser (spec M31). The scheme guard below pins them.
_SDK_SCHEMES = {"http", "https", "ws", "wss", "mem", "memory", "file", "surrealkv", "surrealkv+versioned"}

_EMBEDDED_URLS = [
    "mem://",
    "memory://",
    "mem://x",
    "surrealkv:///tmp/x",
    "surrealkv+versioned:///tmp/x",
    "file:///tmp/x",
    "surrealkv://relative",
]
_SERVER_URLS = ["ws://h:1/rpc", "wss://h", "http://h:8000", "https://h"]
_REFUSED_URLS = ["rocksdb:///x", "memory", "not a url", "htp://h"]


def _count_constructions(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the URL of every SDK client the connection manager constructs, then construct it as usual."""
    calls: list[str] = []
    real = connection_manager.AsyncSurreal

    def recording(url: str, *args: Any, **kwargs: Any) -> Any:
        calls.append(url)
        return real(url, *args, **kwargs)

    monkeypatch.setattr(connection_manager, "AsyncSurreal", recording)
    return calls


@pytest.fixture
def _fresh_url_cache() -> Iterator[None]:
    """``url_kind`` is cached: a test that fakes the SDK's parser starts and ends with an empty cache."""
    url_kind.cache_clear()
    yield
    url_kind.cache_clear()


class TestEmbeddedUrl:
    """AC-1, AC-2: the SDK's own parser decides what is embedded; the ORM keeps no list of embedded schemes."""

    @pytest.mark.parametrize("url", _EMBEDDED_URLS)
    def test_embedded_urls(self, url: str) -> None:
        assert is_embedded_url(url) is True

    @pytest.mark.parametrize("url", [*_SERVER_URLS, "memory", "rocksdb:///x", "", None, "not a url"])
    def test_not_embedded_urls(self, url: str | None) -> None:
        assert is_embedded_url(url) is False

    def test_url_kind(self) -> None:
        for url in _SERVER_URLS:
            assert url_kind(url) == "server", url
        for url in _EMBEDDED_URLS:
            assert url_kind(url) == "embedded", url
        for url in _REFUSED_URLS:
            assert url_kind(url) == "refused", url

    @pytest.mark.usefixtures("_fresh_url_cache")
    def test_a_scheme_the_sdk_adds_is_embedded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """#233: once a released SDK lists ``rocksdb``, the ORM treats it as embedded with no change."""
        fake_scheme = enum.Enum(
            "FakeScheme",
            {"HTTP": "http", "HTTPS": "https", "WS": "ws", "WSS": "wss", "MEM": "mem", "ROCKSDB": "rocksdb"},
        )

        class FakeUrl:
            def __init__(self, url: str) -> None:
                self.scheme = fake_scheme(urlparse(url).scheme)

        monkeypatch.setattr(embedded, "SdkUrlScheme", fake_scheme)
        monkeypatch.setattr(embedded, "SdkUrl", FakeUrl)
        assert is_embedded_url("rocksdb:///x")
        assert not is_embedded_url("ws://h")
        assert url_kind("ws://h") == "server"

    @pytest.mark.usefixtures("_fresh_url_cache")
    def test_a_scheme_of_another_type_is_a_server_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A parser answer the ORM does not recognise keeps the v0.24.1 (server) code path."""

        class OddUrl:
            def __init__(self, url: str) -> None:
                self.scheme = urlparse(url).scheme

        monkeypatch.setattr(embedded, "SdkUrl", OddUrl)
        assert url_kind("mem://") == "server"
        assert not is_embedded_url("mem://")

    def test_scheme_guard(self) -> None:
        """Spec D10: a new SDK scheme fails here until the classification is reviewed."""
        assert {s.value for s in _sdk.SdkUrlScheme} == _SDK_SCHEMES, (
            "the surrealdb SDK changed its URL schemes: review embedded.url_kind (spec D10) — a new network "
            "scheme would be classified as embedded; rocksdb (#233) needs tests and docs"
        )

    @pytest.mark.parametrize("url", [None, "", "mem://", "ws://h/rpc", "surrealkv:///tmp/x"])
    def test_require_supported_url_accepts(self, url: str | None) -> None:
        assert require_supported_url(url) is None

    def test_require_supported_url_refuses_rocksdb(self) -> None:
        """E11: the SDK's own refusal, relayed, naming #233; never retried."""
        with pytest.raises(SurrealDbConnectionError) as info:
            require_supported_url("rocksdb:///x")
        exc = info.value
        text = str(exc)
        assert "cannot open" in text
        assert "issues/233" in text
        assert exc.__cause__ is not None
        assert str(exc.__cause__) in text
        assert getattr(exc, "retryable", None) is False
        schemes = ", ".join(sorted(_SDK_SCHEMES))
        assert text == (
            f"The installed surrealdb SDK cannot open 'rocksdb:///x': {exc.__cause__}. The URL schemes it "
            f"accepts are: {schemes}. Embedded RocksDB (rocksdb://) is not supported by the official SDK yet: "
            "see https://github.com/EulogySnowfall/SurrealDB-ORM-lite/issues/233."
        )

    @pytest.mark.parametrize("url", ["htp://h", "memory", "not a url", "rocksdb://[x"])
    def test_require_supported_url_refuses_others_without_the_rocksdb_hint(self, url: str) -> None:
        """E11 without the #233 hint (``rocksdb://[x``: the URL itself cannot be split, so no hint)."""
        with pytest.raises(SurrealDbConnectionError) as info:
            require_supported_url(url)
        exc = info.value
        text = str(exc)
        assert "cannot open" in text
        assert "233" not in text
        assert exc.__cause__ is not None
        assert str(exc.__cause__) in text
        assert getattr(exc, "retryable", None) is False
        assert text.endswith(f"The URL schemes it accepts are: {', '.join(sorted(_SDK_SCHEMES))}.")

    def test_rocksdb_hint_ignores_the_scheme_case(self) -> None:
        with pytest.raises(SurrealDbConnectionError, match="issues/233"):
            require_supported_url("RocksDB:///x")

    def test_message_builders(self) -> None:
        """E1–E4: the exact texts of spec §5.5, built in one place."""
        assert embedded.live_unsupported("mem://") == (
            "Live queries need a WebSocket server connection (ws:// or wss://): the embedded engine ('mem://') "
            "has no notification channel to deliver them. Read the table's change feed with "
            "Model.objects().changes() instead, or connect to a server."
        )
        assert embedded.ephemeral_unsupported("mem://") == (
            "ephemeral_client() opens a second session on the configured database, which the embedded engine "
            "('mem://') cannot do: every in-process client is a database of its own. Use the shared client "
            "(get_client()), or connect to a server."
        )
        assert embedded.model_auth_unsupported("User", "signin", "mem://") == (
            "User.signin() runs on a session of its own so that the shared connection keeps its identity, and "
            "the embedded engine ('mem://') has a single session per database. Pass bind=True to sign that "
            "session in (every model then runs as this user, in every event loop of the process), or connect "
            "to a server."
        )
        assert embedded.changes_since_datetime_unsupported() == (
            "changes(since=<datetime>) is not available on the embedded engine of the surrealdb SDK 2.x: it keeps "
            "no timestamps for change-feed positions, so a datetime cannot be resolved. Pass a cursor "
            "(ChangeModelStream.cursor) or since=None."
        )

    async def test_is_embedded_follows_the_configured_url(self) -> None:
        await SurrealDBConnectionManager.unset_connection()
        try:
            assert SurrealDBConnectionManager.is_embedded() is False
            SurrealDBConnectionManager.set_connection("ws://h/rpc", "root", "root", "ns", "db")
            assert SurrealDBConnectionManager.is_embedded() is False
            SurrealDBConnectionManager.set_connection("mem://", "root", "root", "ns", "db")
            assert SurrealDBConnectionManager.is_embedded() is True
            SurrealDBConnectionManager.set_connection("rocksdb:///x", "root", "root", "ns", "db")
            assert SurrealDBConnectionManager.is_embedded() is False
        finally:
            await SurrealDBConnectionManager.unset_connection()

    def test_embedded_module_imports_no_other_orm_module(self) -> None:
        """Spec §6.2: a leaf module — ``_sdk``, ``exceptions`` and the standard library only."""
        tree = ast.parse(inspect.getsource(embedded))
        relative: set[str | None] = set()
        absolute: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level:
                    relative.add(node.module)
                else:
                    absolute.add((node.module or "").split(".")[0])
            elif isinstance(node, ast.Import):
                absolute.update(alias.name.split(".")[0] for alias in node.names)
        assert relative <= {"_sdk", "exceptions"}, relative
        assert absolute <= set(sys.stdlib_module_names), absolute - set(sys.stdlib_module_names)
        # #233, D10: ``rocksdb`` lives in E11's hint only, never in the classification.
        assert "rocksdb" not in inspect.getsource(embedded.url_kind)


class TestSetConnectionCredentials:
    """AC-3 (D1, D2): credentials are optional on an embedded URL; every existing call behaves as before."""

    async def test_positional_call_unchanged(self) -> None:
        try:
            SurrealDBConnectionManager.set_connection("ws://h/rpc", "root", "root", "ns", "db")
            assert SurrealDBConnectionManager.get_connection_kwargs() == {
                "url": "ws://h/rpc",
                "user": "root",
                "namespace": "ns",
                "database": "db",
            }
            assert SurrealDBConnectionManager.is_connection_set() is True
        finally:
            await SurrealDBConnectionManager.unset_connection()

    async def test_keyword_call_unchanged(self) -> None:
        try:
            SurrealDBConnectionManager.set_connection(
                url="ws://h/rpc", user="root", password="root", namespace="ns", database="db"
            )
            assert SurrealDBConnectionManager.get_connection_kwargs() == {
                "url": "ws://h/rpc",
                "user": "root",
                "namespace": "ns",
                "database": "db",
            }
            assert SurrealDBConnectionManager.is_connection_set() is True
        finally:
            await SurrealDBConnectionManager.unset_connection()

    async def test_embedded_url_without_credentials_is_set(self) -> None:
        try:
            SurrealDBConnectionManager.set_connection(url="mem://", namespace="t", database="t")
            assert SurrealDBConnectionManager.is_connection_set() is True
            assert SurrealDBConnectionManager.get_user() is None
            assert not SurrealDBConnectionManager.is_password_set()
        finally:
            await SurrealDBConnectionManager.unset_connection()

    async def test_embedded_url_keeps_credentials(self) -> None:
        """D2: kept (one configuration switches server <-> mem:// by URL alone), never sent to the engine."""
        try:
            SurrealDBConnectionManager.set_connection(url="mem://", user="root", password="root", namespace="t", database="t")
            assert SurrealDBConnectionManager.is_connection_set() is True
            assert SurrealDBConnectionManager.get_user() == "root"
            assert SurrealDBConnectionManager.is_password_set()
        finally:
            await SurrealDBConnectionManager.unset_connection()

    async def test_server_url_without_credentials_is_not_set(self) -> None:
        try:
            SurrealDBConnectionManager.set_connection(url="ws://h/rpc", namespace="t", database="t")
            assert SurrealDBConnectionManager.is_connection_set() is False
            with pytest.raises(ValueError) as info:
                await SurrealDBConnectionManager.get_client()
            assert str(info.value) == "Connection not been set."
        finally:
            await SurrealDBConnectionManager.unset_connection()

    async def test_embedded_url_without_selection_is_not_set(self) -> None:
        try:
            SurrealDBConnectionManager.set_connection(url="mem://")
            assert SurrealDBConnectionManager.is_connection_set() is False
        finally:
            await SurrealDBConnectionManager.unset_connection()

    def test_signature(self) -> None:
        """D1: positional order unchanged; the four settings after the URL default to ``None``."""
        parameters = inspect.signature(SurrealDBConnectionManager.set_connection).parameters
        assert list(parameters) == ["url", "user", "password", "namespace", "database"]
        assert parameters["url"].default is inspect.Parameter.empty
        for name in ("user", "password", "namespace", "database"):
            assert parameters[name].default is None, name


def _set_refused(url: str, *, credentials: bool) -> None:
    if credentials:
        SurrealDBConnectionManager.set_connection(url=url, user="root", password="root", namespace="t", database="t")
    else:
        SurrealDBConnectionManager.set_connection(url=url, namespace="t", database="t")


def _assert_e11(exc: BaseException, *, rocksdb: bool) -> None:
    assert isinstance(exc, SurrealDbConnectionError)
    assert not isinstance(exc, ValueError)
    text = str(exc)
    assert "cannot open" in text
    assert ("issues/233" in text) is rocksdb
    if not rocksdb:
        assert "233" not in text
    assert exc.__cause__ is not None
    assert str(exc.__cause__) in text
    assert getattr(exc, "retryable", None) is False


class TestRefusedUrl:
    """AC-19 (D15), no server: a URL the SDK's parser refuses gets E11, before anything is constructed."""

    @pytest.mark.parametrize("credentials", [False, True], ids=["no-credentials", "credentials"])
    async def test_get_client_raises_e11(self, monkeypatch: pytest.MonkeyPatch, credentials: bool) -> None:
        calls = _count_constructions(monkeypatch)
        try:
            _set_refused("rocksdb:///x", credentials=credentials)
            with pytest.raises(SurrealDbConnectionError) as info:
                await SurrealDBConnectionManager.get_client()
            _assert_e11(info.value, rocksdb=True)
            assert calls == []
        finally:
            await SurrealDBConnectionManager.unset_connection()

    @pytest.mark.parametrize("credentials", [False, True], ids=["no-credentials", "credentials"])
    async def test_ephemeral_client_raises_e11(self, monkeypatch: pytest.MonkeyPatch, credentials: bool) -> None:
        calls = _count_constructions(monkeypatch)
        try:
            _set_refused("rocksdb:///x", credentials=credentials)
            with pytest.raises(SurrealDbConnectionError) as info:
                async with SurrealDBConnectionManager.ephemeral_client():
                    pytest.fail("ephemeral_client() opened a client on a refused URL")  # pragma: no cover
            _assert_e11(info.value, rocksdb=True)
            assert calls == []
        finally:
            await SurrealDBConnectionManager.unset_connection()

    @pytest.mark.parametrize("credentials", [False, True], ids=["no-credentials", "credentials"])
    async def test_validate_connection_is_false(self, monkeypatch: pytest.MonkeyPatch, credentials: bool) -> None:
        calls = _count_constructions(monkeypatch)
        try:
            _set_refused("rocksdb:///x", credentials=credentials)
            assert await SurrealDBConnectionManager.validate_connection() is False
            assert SurrealDBConnectionManager.is_embedded() is False
            assert calls == []
        finally:
            await SurrealDBConnectionManager.unset_connection()

    @pytest.mark.parametrize("credentials", [False, True], ids=["no-credentials", "credentials"])
    async def test_a_typo_gets_e11_without_the_rocksdb_hint(self, monkeypatch: pytest.MonkeyPatch, credentials: bool) -> None:
        calls = _count_constructions(monkeypatch)
        try:
            _set_refused("htp://h", credentials=credentials)
            with pytest.raises(SurrealDbConnectionError) as info:
                await SurrealDBConnectionManager.get_client()
            _assert_e11(info.value, rocksdb=False)
            assert calls == []
        finally:
            await SurrealDBConnectionManager.unset_connection()

    async def test_require_connection_raises_e11_first(self) -> None:
        """E11 whatever else is missing: ``require_connection()`` covers ephemeral clients and model auth."""
        try:
            SurrealDBConnectionManager.set_connection(url="rocksdb:///x")
            assert SurrealDBConnectionManager.is_connection_set() is False
            with pytest.raises(SurrealDbConnectionError) as info:
                SurrealDBConnectionManager.require_connection()
            _assert_e11(info.value, rocksdb=True)
        finally:
            await SurrealDBConnectionManager.unset_connection()


class TestRefusedUrlCachedClientE2E:
    """AC-19 on a server (both lines): E11 is checked before the per-loop fast path, so a cached client is
    not handed back once the URL changed to one the SDK refuses."""

    async def test_e11_wins_over_the_cached_server_client(self) -> None:
        try:
            SurrealDBConnectionManager.set_connection(
                url=_ws_url(), user="root", password="root", namespace="ns", database="db"
            )
            server = await SurrealDBConnectionManager.get_client()
            assert server is not None
            await SurrealDBConnectionManager.set_url("rocksdb:///x")
            with pytest.raises(SurrealDbConnectionError) as info:
                await SurrealDBConnectionManager.get_client()
            _assert_e11(info.value, rocksdb=True)
        finally:
            await SurrealDBConnectionManager.unset_connection()
