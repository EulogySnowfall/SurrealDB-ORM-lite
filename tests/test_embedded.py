"""The official SDK's in-process SurrealDB engine (v0.25.0): ``mem://``, ``surrealkv://<path>`` and the
other schemes the SDK routes to it.

Unit tests first (URL classification, error texts: no engine, no server); the classes that open an engine
or a server are suffixed ``E2E``. Most E2E classes need no server at all: the engine runs in-process.
"""

import ast
import asyncio
import contextlib
import enum
import inspect
import sys
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlparse

import pytest
import surrealdb
from surrealdb.connections.async_embedded import AsyncEmbeddedSurrealConnection

from surreal_orm_lite import BaseSurrealModel, SurrealConfigDict, SurrealDBConnectionManager, _sdk, connection_manager
from surreal_orm_lite import embedded as embedded_module
from surreal_orm_lite.embedded import is_embedded_url, require_supported_url, url_kind
from surreal_orm_lite.exceptions import SurrealDbAuthenticationError, SurrealDbConnectionError
from tests.conftest import _ws_url

_T = TypeVar("_T")


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


#: One recorded call of an embedded client's method: ``(method, id(client), args, kwargs)``.
_Call = tuple[str, int, tuple[Any, ...], dict[str, Any]]


def _record_calls(monkeypatch: pytest.MonkeyPatch, *methods: str) -> list[_Call]:
    """Record the calls of the SDK embedded client's *methods*, then run them as usual.

    Patched on the class, and recording ``id(client)`` rather than the client: an instance attribute
    wrapping a bound method would make the client reference itself, so ``del client`` would no longer
    end its engine (the closed-client rule of spec §5.2.4).
    """
    calls: list[_Call] = []
    for name in methods:
        real = getattr(AsyncEmbeddedSurrealConnection, name)

        def recording(real: Any = real, name: str = name) -> Any:
            async def method(self: Any, *args: Any, **kwargs: Any) -> Any:
                calls.append((name, id(self), args, kwargs))
                return await real(self, *args, **kwargs)

            return method

        monkeypatch.setattr(AsyncEmbeddedSurrealConnection, name, recording())
    return calls


def _calls_of(calls: list[_Call], client: Any, *methods: str) -> list[tuple[str, tuple[Any, ...]]]:
    """``(method, args)`` of the recorded calls made on *client* (all methods when none is named)."""
    return [(name, args) for name, owner, args, _ in calls if owner == id(client) and (not methods or name in methods)]


def _failing(monkeypatch: pytest.MonkeyPatch, method: str) -> list[bool]:
    """Make the embedded client's *method* raise ``RuntimeError("boom")`` while ``switch[0]`` is true."""
    switch = [True]
    real = getattr(AsyncEmbeddedSurrealConnection, method)

    async def maybe_failing(self: Any, *args: Any, **kwargs: Any) -> Any:
        if switch[0]:
            raise RuntimeError("boom")
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(AsyncEmbeddedSurrealConnection, method, maybe_failing)
    return switch


@contextlib.asynccontextmanager
async def embedded(url: str = "mem://", *, namespace: str = "t", database: str = "t") -> AsyncIterator[Any]:
    """Configure *url* without credentials, yield the slot's client, and unset everything afterwards.

    A test that reopens a ``surrealkv://`` path drops its ``as client`` binding first (``del client``).
    """
    SurrealDBConnectionManager.set_connection(url=url, namespace=namespace, database=database)
    try:
        yield await SurrealDBConnectionManager.get_client()
    finally:
        await SurrealDBConnectionManager.unset_connection()


async def _in_new_loop(fn: Callable[[], Coroutine[Any, Any, _T]]) -> _T:
    """Run ``fn()`` in a new event loop on another thread, without blocking the test's loop."""
    return await asyncio.to_thread(lambda: asyncio.run(fn()))


def _signatures() -> dict[tuple[str | None, str | None, str | None, str], tuple[str, ...]]:
    """The manager's stored-function signature cache (white box)."""
    cache: dict[tuple[str | None, str | None, str | None, str], tuple[str, ...]]
    cache = SurrealDBConnectionManager._SurrealDBConnectionManager__function_signatures  # type: ignore[attr-defined]
    return cache


def _slot_client() -> Any:
    """The manager's embedded slot (white box): its client, or ``None``."""
    return getattr(SurrealDBConnectionManager, "_SurrealDBConnectionManager__embedded_client", None)


def _mark_selection_stale() -> None:
    """White box: what a failed selection reset leaves behind."""
    SurrealDBConnectionManager._SurrealDBConnectionManager__embedded_selection = None  # type: ignore[attr-defined]


#: A record access and its table, on the database ``t`` of the engine (spec AC-4).
_ACCOUNT_DDL = (
    "DEFINE ACCESS account ON DATABASE TYPE RECORD "
    "SIGNUP (CREATE user SET email = $email, pass = crypto::argon2::generate($pass)) "
    "SIGNIN (SELECT * FROM user WHERE email = $email AND crypto::argon2::compare(pass, $pass)) "
    "DURATION FOR SESSION 1h; "
    "DEFINE TABLE user SCHEMALESS PERMISSIONS FOR select, update, delete WHERE id = $auth.id;"
)
_ACCOUNT = {"email": "a@x", "pass": "p"}


class Person(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="person")

    id: str | None = None
    name: str
    age: int = 0


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

        monkeypatch.setattr(embedded_module, "SdkUrlScheme", fake_scheme)
        monkeypatch.setattr(embedded_module, "SdkUrl", FakeUrl)
        assert is_embedded_url("rocksdb:///x")
        assert not is_embedded_url("ws://h")
        assert url_kind("ws://h") == "server"

    @pytest.mark.usefixtures("_fresh_url_cache")
    def test_a_scheme_of_another_type_is_a_server_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A parser answer the ORM does not recognise keeps the v0.24.1 (server) code path."""

        class OddUrl:
            def __init__(self, url: str) -> None:
                self.scheme = urlparse(url).scheme

        monkeypatch.setattr(embedded_module, "SdkUrl", OddUrl)
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
        assert embedded_module.live_unsupported("mem://") == (
            "Live queries need a WebSocket server connection (ws:// or wss://): the embedded engine ('mem://') "
            "has no notification channel to deliver them. Read the table's change feed with "
            "Model.objects().changes() instead, or connect to a server."
        )
        assert embedded_module.ephemeral_unsupported("mem://") == (
            "ephemeral_client() opens a second session on the configured database, which the embedded engine "
            "('mem://') cannot do: every in-process client is a database of its own. Use the shared client "
            "(get_client()), or connect to a server."
        )
        assert embedded_module.model_auth_unsupported("User", "signin", "mem://") == (
            "User.signin() runs on a session of its own so that the shared connection keeps its identity, and "
            "the embedded engine ('mem://') has a single session per database. Pass bind=True to sign that "
            "session in (every model then runs as this user, in every event loop of the process), or connect "
            "to a server."
        )
        assert embedded_module.changes_since_datetime_unsupported() == (
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
        tree = ast.parse(inspect.getsource(embedded_module))
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
        assert "rocksdb" not in inspect.getsource(embedded_module.url_kind)


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


class TestEmbeddedOpenE2E:
    """AC-4 (D2), no server: the engine is opened with ``connect()`` and never signed in as the configured user."""

    async def test_open_never_signs_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _record_calls(monkeypatch, "signin", "connect", "use")
        urls = _count_constructions(monkeypatch)
        SurrealDBConnectionManager.set_connection(url="mem://", user="root", password="root", namespace="t", database="t")
        try:
            client = await SurrealDBConnectionManager.get_client()
            assert type(client).__name__ == "AsyncEmbeddedSurrealConnection"
            assert urls == ["mem://"]
            assert _calls_of(calls, client, "signin") == []
            assert [(name, args, kwargs) for name, owner, args, kwargs in calls if name == "connect"] == [("connect", (), {})]
            assert _calls_of(calls, client, "connect", "use") == [("connect", ()), ("use", ("t", "t"))]
            await Person(id="ada", name="Ada", age=36).save()
            got = await Person.objects().get("ada")
            assert (got.name, got.age) == ("Ada", 36)
        finally:
            await SurrealDBConnectionManager.unset_connection()

    async def test_a_stored_session_is_replayed_after_a_close_on_surrealkv(self, tmp_path: Path) -> None:
        async with embedded(f"surrealkv://{tmp_path / 'kv'}") as client:
            await client.query(_ACCOUNT_DDL)
            tokens = await SurrealDBConnectionManager.signup(access="account", variables=_ACCOUNT)
            del client  # the closed-client rule: the path is reopened below
            await SurrealDBConnectionManager.close_connection()
            assert not SurrealDBConnectionManager.is_connected()
            me = await SurrealDBConnectionManager.info()
            assert me is not None
            assert me["email"] == "a@x"
            assert SurrealDBConnectionManager.get_session_token() == tokens.access

    async def test_a_stored_session_is_refused_then_forgotten_on_a_new_mem_database(self) -> None:
        async with embedded() as client:
            await client.query(_ACCOUNT_DDL)
            await SurrealDBConnectionManager.signup(access="account", variables=_ACCOUNT)
            assert SurrealDBConnectionManager.get_session_token() is not None
            await SurrealDBConnectionManager.close_connection()
            with pytest.raises(SurrealDbAuthenticationError):
                await SurrealDBConnectionManager.get_client()
            assert SurrealDBConnectionManager.get_session_token() is None
            again = await SurrealDBConnectionManager.get_client()
            assert again is not client
            assert await SurrealDBConnectionManager.info() is None  # the in-process owner
            assert await Person.objects().count() == 0

    async def test_a_bad_path_raises_e5(self) -> None:
        url = "surrealkv:///proc/forbidden/kv"
        SurrealDBConnectionManager.set_connection(url=url, namespace="t", database="t")
        try:
            with pytest.raises(SurrealDbConnectionError, match="Can't open the embedded database") as info:
                await SurrealDBConnectionManager.get_client()
            exc = info.value
            assert getattr(exc, "retryable", None) is False
            assert exc.__cause__ is not None
            assert str(exc) == f"Can't open the embedded database {url!r}: {exc.__cause__}"
            assert not SurrealDBConnectionManager.is_connected()
            assert _slot_client() is None
        finally:
            await SurrealDBConnectionManager.unset_connection()

    async def test_a_failed_selection_at_open_closes_the_half_opened_engine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _record_calls(monkeypatch, "close")
        _failing(monkeypatch, "use")
        SurrealDBConnectionManager.set_connection(url="mem://", namespace="t", database="t")
        try:
            with pytest.raises(SurrealDbConnectionError, match="Can't open the embedded database 'mem://': boom"):
                await SurrealDBConnectionManager.get_client()
            assert [name for name, *_ in calls] == ["close"]
            assert _slot_client() is None
        finally:
            await SurrealDBConnectionManager.unset_connection()


class TestOneEnginePerProcessE2E:
    """AC-5 a–f, h–k (D3), no server: one embedded client for every event loop and thread of the process."""

    async def test_every_loop_sees_one_database(self) -> None:
        async with embedded() as client:
            await Person(id="main", name="Main").save()

            async def from_another_loop() -> tuple[int, int]:
                other = await SurrealDBConnectionManager.get_client()
                seen = await Person.objects().count()
                await Person(id="thread", name="Thread").save()
                return id(other), seen

            other_id, seen = await _in_new_loop(from_another_loop)
            assert other_id == id(client)
            assert seen == 1
            assert await Person.objects().count() == 2
            assert await SurrealDBConnectionManager.get_client() is client

    async def test_concurrent_first_calls_open_one_engine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        urls = _count_constructions(monkeypatch)
        SurrealDBConnectionManager.set_connection(url="mem://", namespace="t", database="t")
        try:

            async def twenty() -> set[int]:
                clients = await asyncio.gather(*(SurrealDBConnectionManager.get_client() for _ in range(20)))
                return {id(client) for client in clients}

            first, second, *here = await asyncio.gather(
                _in_new_loop(twenty), _in_new_loop(twenty), *(SurrealDBConnectionManager.get_client() for _ in range(20))
            )
            assert urls == ["mem://"]
            assert first | second | {id(client) for client in here} == {id(_slot_client())}
        finally:
            await SurrealDBConnectionManager.unset_connection()

    async def test_a_close_from_another_loop_closes_it_for_every_loop(self) -> None:
        async with embedded() as client:
            await Person(id="ada", name="Ada").save()
            del client
            await _in_new_loop(SurrealDBConnectionManager.close_connection)
            assert not SurrealDBConnectionManager.is_connected()
            assert await Person.objects().count() == 0  # a new, empty mem:// database

    async def test_another_url_closes_the_old_engine(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        calls = _record_calls(monkeypatch, "close")
        async with embedded() as client:
            SurrealDBConnectionManager.set_connection(url=f"surrealkv://{tmp_path / 'kv'}", namespace="t", database="t")
            new = await SurrealDBConnectionManager.get_client()
            assert new is not client
            assert _calls_of(calls, client) == [("close", ())]
            assert _calls_of(calls, new) == []
            del new

    async def test_another_database_resets_the_session_on_the_same_engine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async with embedded(database="a") as client:
            await Person(id="ada", name="Ada").save()
            calls = _record_calls(monkeypatch, "invalidate", "use", "close")
            SurrealDBConnectionManager.set_connection(url="mem://", namespace="t", database="b")
            assert await SurrealDBConnectionManager.get_client() is client
            assert _calls_of(calls, client) == [("invalidate", ()), ("use", ("t", "b"))]
            assert await Person.objects().count() == 0
            SurrealDBConnectionManager.set_connection(url="mem://", namespace="t", database="a")
            assert await Person.objects().count() == 1
            assert await SurrealDBConnectionManager.get_client() is client

    async def test_validate_connection_keeps_the_database(self, monkeypatch: pytest.MonkeyPatch) -> None:
        urls = _count_constructions(monkeypatch)
        async with embedded() as client:
            await Person(id="ada", name="Ada").save()
            assert await SurrealDBConnectionManager.validate_connection() is True
            assert await SurrealDBConnectionManager.set_database("t", reconnect=True) is True
            assert await SurrealDBConnectionManager.get_client() is client
            assert await Person.objects().count() == 1
            assert urls == ["mem://"]

    async def test_validate_connection_opens_the_engine(self) -> None:
        SurrealDBConnectionManager.set_connection(url="mem://", namespace="t", database="t")
        try:
            assert await SurrealDBConnectionManager.validate_connection() is True
            assert SurrealDBConnectionManager.is_connected()
        finally:
            await SurrealDBConnectionManager.unset_connection()

    async def test_validate_connection_is_false_when_the_engine_cannot_open(self) -> None:
        SurrealDBConnectionManager.set_connection(url="surrealkv:///proc/forbidden/kv", namespace="t", database="t")
        try:
            assert await SurrealDBConnectionManager.validate_connection() is False
        finally:
            await SurrealDBConnectionManager.unset_connection()

    async def test_is_connected_follows_the_slot(self) -> None:
        SurrealDBConnectionManager.set_connection(url="mem://", namespace="t", database="t")
        try:
            assert SurrealDBConnectionManager.is_connected() is False
            assert await asyncio.to_thread(SurrealDBConnectionManager.is_connected) is False
            await SurrealDBConnectionManager.get_client()
            assert SurrealDBConnectionManager.is_connected() is True
            assert await asyncio.to_thread(SurrealDBConnectionManager.is_connected) is True  # outside any loop
            await SurrealDBConnectionManager.close_connection()
            assert SurrealDBConnectionManager.is_connected() is False
        finally:
            await SurrealDBConnectionManager.unset_connection()

    @pytest.mark.parametrize("closer", ["close_all_connections", "unset_connection"])
    async def test_close_all_and_unset_close_the_slot(self, monkeypatch: pytest.MonkeyPatch, closer: str) -> None:
        calls = _record_calls(monkeypatch, "close")
        SurrealDBConnectionManager.set_connection(url="mem://", namespace="t", database="t")
        try:
            client = await SurrealDBConnectionManager.get_client()
            await getattr(SurrealDBConnectionManager, closer)()
            assert SurrealDBConnectionManager.is_connected() is False
            assert _slot_client() is None
            assert _calls_of(calls, client) == [("close", ())]
        finally:
            await SurrealDBConnectionManager.unset_connection()

    async def test_an_embedded_close_clears_the_function_signature_cache(self) -> None:
        async with embedded():
            _signatures()[("mem://", "t", "t", "fn::x")] = ("a",)
            await SurrealDBConnectionManager.close_connection()
            assert SurrealDBConnectionManager.function_signature_cache_size() == 0

    async def test_a_server_close_with_no_engine_keeps_the_function_signature_cache(self) -> None:
        """v0.24.1 behaviour: with the slot empty, close_connection() changes nothing it did not change before."""
        SurrealDBConnectionManager.set_connection("ws://127.0.0.1:1/rpc", "root", "root", "ns", "db")
        try:
            _signatures()[("mem://", "t", "t", "fn::x")] = ("a",)
            await SurrealDBConnectionManager.close_connection()
            assert SurrealDBConnectionManager.function_signature_cache_size() == 1
        finally:
            SurrealDBConnectionManager.clear_function_signature_cache()
            await SurrealDBConnectionManager.unset_connection()

    async def test_a_url_changed_while_waiting_for_the_guard_is_served_as_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A server URL configured by another thread meanwhile is never opened into the embedded slot."""
        real_guard = SurrealDBConnectionManager._embedded_guard

        @contextlib.asynccontextmanager
        async def switching_guard() -> AsyncIterator[None]:
            SurrealDBConnectionManager.set_connection("ws://127.0.0.1:1/rpc", "root", "root", "ns", "db")
            async with real_guard():
                yield

        SurrealDBConnectionManager.set_connection(url="mem://", namespace="t", database="t")
        urls = _count_constructions(monkeypatch)
        monkeypatch.setattr(SurrealDBConnectionManager, "_embedded_guard", switching_guard)
        try:
            with pytest.raises(SurrealDbConnectionError, match="Can't connect to the database"):
                await SurrealDBConnectionManager.get_client()
            assert urls == ["ws://127.0.0.1:1/rpc"]
            assert _slot_client() is None
        finally:
            await SurrealDBConnectionManager.unset_connection()


class TestSelectionResetE2E:
    """AC-20 (D16, D18), no server: a namespace/database change returns the shared session to the in-process
    owner and ends the stored session; a failed reset raises E12 and keeps the engine."""

    async def test_a_database_change_after_a_signup_returns_to_the_owner(self) -> None:
        async with embedded() as client:
            await client.query(_ACCOUNT_DDL)
            await SurrealDBConnectionManager.signup(access="account", variables=_ACCOUNT)
            me = await SurrealDBConnectionManager.info()
            assert me is not None
            assert me["email"] == "a@x"

            SurrealDBConnectionManager.set_connection(url="mem://", namespace="t", database="other")
            assert await SurrealDBConnectionManager.get_client() is client
            assert await SurrealDBConnectionManager.info() is None
            await Person(id="bob", name="Bob").save()
            assert (await Person.objects().get("bob")).name == "Bob"

            SurrealDBConnectionManager.set_connection(url="mem://", namespace="t", database="t")
            assert await SurrealDBConnectionManager.get_client() is client
            assert await SurrealDBConnectionManager.info() is None
            rows = await client.query("SELECT email FROM user;")
            assert [row["email"] for row in rows] == ["a@x"]

    @pytest.mark.parametrize("setter", ["set_database", "set_namespace"])
    async def test_set_database_or_namespace_ends_the_stored_session(self, setter: str) -> None:
        async with embedded() as client:
            await client.query(_ACCOUNT_DDL)
            await SurrealDBConnectionManager.signup(access="account", variables=_ACCOUNT)
            await SurrealDBConnectionManager.signin(access="account", variables=_ACCOUNT)
            assert SurrealDBConnectionManager.get_session_token() is not None
            assert await getattr(SurrealDBConnectionManager, setter)("other") is True
            assert SurrealDBConnectionManager.get_session_token() is None
            assert await SurrealDBConnectionManager.info() is None
            await Person(id="bob", name="Bob").save()
            assert await Person.objects().count() == 1
            assert await SurrealDBConnectionManager.get_client() is client

    @pytest.mark.parametrize("setter", ["set_database", "set_namespace"])
    async def test_the_same_value_keeps_the_stored_session(self, setter: str) -> None:
        async with embedded() as client:
            await client.query(_ACCOUNT_DDL)
            tokens = await SurrealDBConnectionManager.signup(access="account", variables=_ACCOUNT)
            assert await getattr(SurrealDBConnectionManager, setter)("t") is True
            assert SurrealDBConnectionManager.get_session_token() == tokens.access
            me = await SurrealDBConnectionManager.info()
            assert me is not None
            assert me["email"] == "a@x"

    async def test_a_server_url_keeps_the_stored_session_on_a_database_change(self) -> None:
        """§14.5: unchanged for server URLs in v0.25.0."""
        SurrealDBConnectionManager.set_connection("ws://127.0.0.1:1/rpc", "root", "root", "ns", "db")
        try:
            SurrealDBConnectionManager._SurrealDBConnectionManager__session_token = "s"  # type: ignore[attr-defined]
            assert await SurrealDBConnectionManager.set_database("other") is True
            assert await SurrealDBConnectionManager.set_namespace("other") is True
            assert SurrealDBConnectionManager.get_session_token() == "s"
        finally:
            await SurrealDBConnectionManager.unset_connection()

    async def test_a_failed_reset_raises_e12_and_keeps_the_engine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _record_calls(monkeypatch, "close")
        async with embedded() as client:
            await Person(id="ada", name="Ada").save()
            failing = _failing(monkeypatch, "use")
            SurrealDBConnectionManager.set_connection(url="mem://", namespace="t", database="other")
            with pytest.raises(SurrealDbConnectionError, match="Can't select namespace") as info:
                await SurrealDBConnectionManager.get_client()
            exc = info.value
            assert str(exc) == "Can't select namespace 't' and database 'other' on the embedded database 'mem://': boom"
            assert getattr(exc, "retryable", None) is False
            assert isinstance(exc.__cause__, RuntimeError)
            assert str(exc.__cause__) == "boom"
            assert await SurrealDBConnectionManager.validate_connection() is False
            assert SurrealDBConnectionManager.is_connected() is True
            assert _calls_of(calls, client) == []

            failing[0] = False
            assert await SurrealDBConnectionManager.get_client() is client
            assert await Person.objects().count() == 0  # the database "other"
            SurrealDBConnectionManager.set_connection(url="mem://", namespace="t", database="t")
            assert await Person.objects().count() == 1
            assert await SurrealDBConnectionManager.get_client() is client

    async def test_a_stale_selection_replays_the_stored_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """What a model-auth failure relies on (spec §6.6): the reset puts the stored identity back."""
        async with embedded() as client:
            await client.query(_ACCOUNT_DDL)
            await SurrealDBConnectionManager.signup(access="account", variables=_ACCOUNT)
            calls = _record_calls(monkeypatch, "invalidate", "use", "authenticate")
            _mark_selection_stale()
            assert await SurrealDBConnectionManager.get_client() is client
            assert [name for name, _ in _calls_of(calls, client)] == ["invalidate", "use", "authenticate"]
            me = await SurrealDBConnectionManager.info()
            assert me is not None
            assert me["email"] == "a@x"

    async def test_restore_embedded_session_puts_the_stored_identity_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async with embedded() as client:
            await client.query(_ACCOUNT_DDL)
            await SurrealDBConnectionManager.signup(access="account", variables=_ACCOUNT)
            await client.invalidate()  # what a refused bind=True leaves behind: not the stored identity
            assert await SurrealDBConnectionManager.info() is None
            calls = _record_calls(monkeypatch, "invalidate", "use", "authenticate")
            await SurrealDBConnectionManager._restore_embedded_session()
            assert [name for name, _ in _calls_of(calls, client)] == ["invalidate", "use", "authenticate"]
            me = await SurrealDBConnectionManager.info()
            assert me is not None
            assert me["email"] == "a@x"

    async def test_restore_embedded_session_without_an_engine_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        urls = _count_constructions(monkeypatch)
        SurrealDBConnectionManager.set_connection(url="mem://", namespace="t", database="t")
        try:
            await SurrealDBConnectionManager._restore_embedded_session()
            assert urls == []
            assert SurrealDBConnectionManager.is_connected() is False
        finally:
            await SurrealDBConnectionManager.unset_connection()

    async def test_a_stale_selection_with_a_refused_token_forgets_it(self) -> None:
        async with embedded() as client:
            SurrealDBConnectionManager._SurrealDBConnectionManager__session_token = "not-a-token"  # type: ignore[attr-defined]
            _mark_selection_stale()
            with pytest.raises(SurrealDbAuthenticationError):
                await SurrealDBConnectionManager.get_client()
            assert SurrealDBConnectionManager.get_session_token() is None
            assert await SurrealDBConnectionManager.get_client() is client
            assert await SurrealDBConnectionManager.info() is None

    async def test_a_connection_failure_during_the_replay_keeps_the_selection_stale(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async with embedded() as client:
            dropped = [True]
            real_replay = SurrealDBConnectionManager._replay_session

            async def replay(cls: Any, target: Any) -> None:
                if dropped[0]:
                    raise SurrealDbConnectionError("The connection dropped while restoring the session.")
                await real_replay(target)

            monkeypatch.setattr(SurrealDBConnectionManager, "_replay_session", classmethod(replay))
            calls = _record_calls(monkeypatch, "invalidate")
            _mark_selection_stale()
            with pytest.raises(SurrealDbConnectionError, match="restoring the session"):
                await SurrealDBConnectionManager.get_client()
            dropped[0] = False
            assert await SurrealDBConnectionManager.get_client() is client
            assert _calls_of(calls, client) == [("invalidate", ()), ("invalidate", ())]  # reset again: still stale


class TestServerClientsUntouchedE2E:
    """AC-5 (g), slot server (both lines): an embedded interlude neither closes nor replaces the loop's server
    client, and the engine waits in its slot while the server URL is configured."""

    async def test_the_server_client_survives_an_embedded_interlude(self) -> None:
        settings: dict[str, Any] = {
            "url": _ws_url(),
            "user": "root",
            "password": "root",
            "namespace": "ns",
            "database": "db",
        }
        try:
            SurrealDBConnectionManager.set_connection(**settings)
            server = await SurrealDBConnectionManager.get_client()

            SurrealDBConnectionManager.set_connection(url="mem://", namespace="t", database="t")
            engine = await SurrealDBConnectionManager.get_client()
            assert engine is not server
            await Person(id="ada", name="Ada").save()
            assert await Person.objects().count() == 1

            SurrealDBConnectionManager.set_connection(**settings)
            assert await SurrealDBConnectionManager.get_client() is server
            assert await server.query("RETURN 1;") == 1
            assert _slot_client() is engine

            SurrealDBConnectionManager.set_connection(url="mem://", namespace="t", database="t")
            assert await SurrealDBConnectionManager.get_client() is engine
            assert await Person.objects().count() == 1
        finally:
            await SurrealDBConnectionManager.unset_connection()
