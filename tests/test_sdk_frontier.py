"""The SDK call frontier guard (v0.50.0): every SDK data call the ORM makes goes through ``surreal_orm_lite._client``.

Two checks keep the frontier honest; this module holds the first one, ``tests/_frontier_spy.py`` the second.

**The AST check** walks ``src/surreal_orm_lite/**/*.py`` except ``_client.py`` and looks at every call made on an *SDK
client expression*:

- an ``await`` of a connection-manager coroutine that returns a client (``FACTORIES``), e.g.
  ``(await cls.get_client()).query(…)``, or of a helper named like a client (``await self._client()``, the migration
  executor's);
- a local name assigned from an SDK client expression in the same function (or an enclosing one), or bound by
  ``async with`` on a connection-manager context that yields one (``CONTEXT_FACTORIES``), as a name or as the first
  element of a tuple;
- a parameter (of the function or an enclosing one, lambdas included) or any attribute named like a client
  (``CLIENT_NAMES``: ``self.client``, ``self._client``, ``tx._client``…).

On such a receiver the ORM may only call the allow-listed session and transaction methods (``ALLOWED``); a data method
(``DATA_METHODS``) belongs in ``_client``, and ``NEVER`` lists methods that act on SDK-registered state the ORM does not
track. Calls on other receivers are not inspected, so the ORM's own ``Model.merge()``, ``Model.patch()``,
``QuerySet.patch()``, ``obj.merge(tx=…)`` or ``variables.update(…)`` never trip it. A ``_client`` CRUD function is never
given a string literal as its target: SDK 3.0 reads every ``str`` target as a table name, so a record is a ``RecordID``
and a table a ``Table``.

``FACTORIES`` and ``CONTEXT_FACTORIES`` are checked complete against ``connection_manager.py``: a new coroutine that
returns a client, or a new context that yields one, fails ``TestFactoryCompleteness`` until the guard knows it.
"""

from __future__ import annotations

import ast
import asyncio
import textwrap
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import pytest

from surreal_orm_lite import BaseSurrealModel, _client, _sdk
from surreal_orm_lite._sdk import RecordID
from surreal_orm_lite.query_set import QuerySet
from tests import _frontier_spy, _sdk_calls

SRC = Path(__file__).resolve().parent.parent / "src" / "surreal_orm_lite"

#: SDK methods that read or write data: only ``_client`` calls them.
DATA_METHODS = frozenset(
    {
        "query",
        "query_raw",
        "select",
        "create",
        "update",
        "upsert",
        "insert",
        "delete",
        "merge",
        "patch",
        "insert_relation",
        "live",
        "subscribe_live",
    }
)
#: SDK methods the ORM never calls: they act on SDK-registered state (sessions, lives, tokens) the ORM does not track.
NEVER = frozenset({"reset", "attach", "detach", "new_session", "refresh", "revoke"})
#: SDK methods callable outside ``_client`` (session, authentication and transaction control).
ALLOWED = frozenset(
    {
        "kill",
        "signin",
        "signup",
        "authenticate",
        "invalidate",
        "use",
        "version",
        "begin",
        "commit",
        "cancel",
        "info",
        "close",
        "connect",
        "let",
        "unset",
        "is_feature_supported",
        "timeout",
    }
)
#: Parameter and attribute names that hold an SDK client.
CLIENT_NAMES = frozenset({"client", "_client", "shared", "opened", "sdk_client"})
#: Connection-manager coroutines that return an SDK client (checked complete by ``TestFactoryCompleteness``).
FACTORIES = frozenset(
    {
        "get_client",
        "reconnect",
        "_acquire_client",
        "_acquire_embedded_client",
        "_held_embedded_client",
        "_open_client",
        "_open_embedded",
        "_connect_for_loop",
        "_replay_then_publish",
    }
)
#: Connection-manager async contexts that yield an SDK client, alone or first in a tuple.
CONTEXT_FACTORIES = frozenset({"_exclusive_embedded_client", "_identity_client", "ephemeral_client"})
#: ``_client`` functions whose second argument is a record or table target (never a ``str``).
TARGET_FUNCTIONS = frozenset({"select", "create", "update", "upsert", "merge", "patch", "delete"})

_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _callee_name(func: ast.expr) -> str | None:
    """The called name: ``f`` for ``f()`` and ``x.f()``, else ``None``."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _own_nodes(scope: ast.AST) -> Iterator[ast.AST]:
    """The nodes of *scope*'s body, without descending into nested functions, lambdas or classes."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (*_SCOPES, ast.ClassDef)):
            stack.extend(ast.iter_child_nodes(node))


def _parameters(scope: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> Iterator[str]:
    args = scope.args
    for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg):
        if arg is not None:
            yield arg.arg


class _FrontierChecker(ast.NodeVisitor):
    """Collects the frontier violations of one module (see the module docstring)."""

    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.violations: list[str] = []
        self._scopes: list[frozenset[str]] = [frozenset()]

    # --- SDK client expressions -------------------------------------------------------------------

    def _is_client(self, node: ast.expr, names: Iterable[str]) -> bool:
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            return _callee_name(node.value.func) in FACTORIES | CLIENT_NAMES
        if isinstance(node, ast.Name):
            return node.id in names
        if isinstance(node, ast.Attribute):
            return node.attr in CLIENT_NAMES
        return False

    def _bound_clients(self, node: ast.AST, names: set[str]) -> Iterator[str]:
        """Names *node* binds to an SDK client."""
        if isinstance(node, ast.Assign) and self._is_client(node.value, names):
            yield from (target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign) and node.value is not None and isinstance(node.target, ast.Name):
            if self._is_client(node.value, names):
                yield node.target.id
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                call = item.context_expr
                if not (isinstance(call, ast.Call) and _callee_name(call.func) in CONTEXT_FACTORIES):
                    continue
                bound = item.optional_vars
                if isinstance(bound, ast.Tuple) and bound.elts:
                    bound = bound.elts[0]
                if isinstance(bound, ast.Name):
                    yield bound.id

    def _enter_scope(self, scope: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> None:
        names = set(self._scopes[-1]) | (CLIENT_NAMES & set(_parameters(scope)))
        own = list(_own_nodes(scope))
        while True:  # aliases of aliases: assign until nothing new is bound
            new = {name for node in own for name in self._bound_clients(node, names)} - names
            if not new:
                break
            names |= new
        self._scopes.append(frozenset(names))
        self.generic_visit(scope)
        self._scopes.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter_scope(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._enter_scope(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._enter_scope(node)

    # --- calls ------------------------------------------------------------------------------------

    def _report(self, node: ast.Call, reason: str) -> None:
        self.violations.append(f"{self.filename}:{node.lineno}: {ast.unparse(node.func)}(): {reason}")

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute):
            method = func.attr
            if self._is_client(func.value, self._scopes[-1]):
                if method in DATA_METHODS:
                    self._report(node, "SDK data call outside surreal_orm_lite._client")
                elif method in NEVER:
                    self._report(node, "the ORM never calls this SDK method")
                elif method not in ALLOWED:
                    self._report(node, "SDK method not allow-listed outside surreal_orm_lite._client")
            elif (
                isinstance(func.value, ast.Name)
                and func.value.id == "_client"
                and method in TARGET_FUNCTIONS
                and len(node.args) >= 2
                and (
                    isinstance(node.args[1], ast.JoinedStr)
                    or (isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str))
                )
            ):
                self._report(node, "str target: pass a RecordID or a Table")
        self.generic_visit(node)


def frontier_violations(source: str, filename: str = "<source>") -> list[str]:
    """Every frontier violation of a module's *source*, one ``file:line: call(): reason`` line each."""
    checker = _FrontierChecker(filename)
    checker.visit(ast.parse(source, filename))
    return checker.violations


def _check(source: str) -> list[str]:
    return frontier_violations(textwrap.dedent(source))


# --- (a) the AST check on synthetic sources ---------------------------------------------------------


class TestAstCheck:
    @pytest.mark.parametrize(
        "source",
        [
            pytest.param(
                """
                async def save(self, obj, tx, qs, variables):
                    await obj.merge(tx=tx)
                    await QuerySet.patch(qs, [])
                    await qs.patch([])
                    variables.update({"a": 1})
                    data.update(other)
                """,
                id="orm-methods-and-plain-objects",
            ),
            pytest.param(
                """
                from surreal_orm_lite import _client

                async def save(cls, client, rid):
                    await _client.query(client, "RETURN 1")
                    await _client.select(client, rid)
                    await _client.insert(client, "person", {})
                    await _client.insert_relation(client, "likes", {})
                    await _client.merge(client, Table("person"), {})
                """,
                id="through-the-frontier",
            ),
            pytest.param(
                """
                async def session(cls, client, token):
                    await client.signin({"username": "u"})
                    await client.authenticate(token)
                    txn = await client.begin()
                    await client.commit(txn)
                    await client.kill(token)
                    client.is_feature_supported(feature)
                    await (await cls.get_client()).use("ns", "db")
                    await client.close()
                """,
                id="allow-listed",
            ),
            pytest.param(
                """
                async def other(cls):
                    client = make_http_session()
                    await client.query("x")
                """,
                id="local-not-from-a-factory",
            ),
        ],
    )
    def test_passes(self, source: str) -> None:
        assert _check(source) == []

    @pytest.mark.parametrize(
        ("source", "call"),
        [
            pytest.param(
                """
                async def save(cls, client, rid, data):
                    await client.merge(rid, data)
                """,
                "client.merge",
                id="parameter",
            ),
            pytest.param(
                """
                async def save(cls):
                    await (await cls.get_client()).query("RETURN 1")
                """,
                "(await cls.get_client()).query",
                id="awaited-factory",
            ),
            pytest.param(
                """
                async def save(cls, rid):
                    c = await cls._acquire_client()
                    c.select(rid)
                """,
                "c.select",
                id="local-from-a-factory",
            ),
            pytest.param(
                """
                async def applied(self):
                    client = await self._client()
                    rows = await client.query("SELECT * FROM t", {})
                """,
                "client.query",
                id="local-from-a-client-helper",
            ),
            pytest.param(
                """
                async def save(cls):
                    async with cls._identity_client() as (x, _):
                        await x.query("RETURN 1")
                """,
                "x.query",
                id="identity-context-tuple",
            ),
            pytest.param(
                """
                async def save(cls):
                    async with cls._exclusive_embedded_client() as x:
                        await x.upsert(rid, {})
                """,
                "x.upsert",
                id="embedded-context",
            ),
            pytest.param(
                """
                async def save(cls, client):
                    await client.reset()
                """,
                "client.reset",
                id="never-reset",
            ),
            pytest.param(
                """
                async def save(self):
                    await self._client.query_raw("RETURN 1")
                """,
                "self._client.query_raw",
                id="attribute",
            ),
            pytest.param(
                """
                async def save(self):
                    alias = self.client
                    other = alias
                    await other.insert_relation("likes", {})
                """,
                "other.insert_relation",
                id="alias-of-an-alias",
            ),
            pytest.param(
                """
                async def outer(cls):
                    shared = await cls.get_client()
                    async def inner():
                        await shared.delete(rid)
                    await inner()
                """,
                "shared.delete",
                id="closure",
            ),
            pytest.param(
                """
                async def outer(cls, client):
                    client.new_session()
                """,
                "client.new_session",
                id="never-new-session",
            ),
            pytest.param(
                """
                async def outer(cls, client):
                    await client.some_new_method()
                """,
                "client.some_new_method",
                id="not-allow-listed",
            ),
            pytest.param(
                """
                async def save(client):
                    await _client.select(client, "person:1")
                """,
                "_client.select",
                id="str-target",
            ),
            pytest.param(
                """
                async def save(client, table):
                    await _client.delete(client, f"{table}")
                """,
                "_client.delete",
                id="f-string-target",
            ),
        ],
    )
    def test_fails(self, source: str, call: str) -> None:
        violations = _check(source)
        assert len(violations) == 1, violations
        assert f": {call}(): " in violations[0]

    def test_a_violation_names_file_and_line(self) -> None:
        violations = frontier_violations("async def f(client):\n    await client.query('x')\n", "pkg/mod.py")
        assert violations == ["pkg/mod.py:2: client.query(): SDK data call outside surreal_orm_lite._client"]

    def test_the_lists_do_not_overlap(self) -> None:
        assert not DATA_METHODS & ALLOWED
        assert not NEVER & ALLOWED
        assert not NEVER & DATA_METHODS


class TestOrmMethodsKeepTheirNames:
    def test_model_merge_and_patch(self) -> None:
        assert callable(BaseSurrealModel.merge)
        assert callable(BaseSurrealModel.patch)

    def test_queryset_patch(self) -> None:
        assert callable(QuerySet.patch)


# --- (b) completeness of the client factories -------------------------------------------------------


def _is_context_manager(node: ast.AsyncFunctionDef) -> bool:
    return any(_callee_name(d.func if isinstance(d, ast.Call) else d) == "asynccontextmanager" for d in node.decorator_list)


def _client_factories(source: str) -> tuple[set[str], set[str]]:
    """The coroutines of *source* that return an SDK client, and the async contexts that yield one.

    A coroutine returns a client when one of its ``return`` values is a name in ``CLIENT_NAMES`` or the ``await`` of a
    coroutine already found (to a fixed point); a context yields one when a ``yield`` value is such an expression, alone
    or first in a tuple. Dunder methods are left out: ``__aenter__`` is the application's
    ``async with SurrealDBConnectionManager()``, which the ORM never enters itself.
    """
    functions = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.AsyncFunctionDef) and not (node.name.startswith("__") and node.name.endswith("__"))
    ]
    found: set[str] = set()

    def returns_client(value: ast.expr | None) -> bool:
        if isinstance(value, ast.Tuple) and value.elts:
            value = value.elts[0]
        if isinstance(value, ast.Name):
            return value.id in CLIENT_NAMES
        if isinstance(value, ast.Await) and isinstance(value.value, ast.Call):
            return _callee_name(value.value.func) in found
        return False

    while True:
        new = {
            fn.name
            for fn in functions
            if not _is_context_manager(fn) and any(returns_client(n.value) for n in _own_nodes(fn) if isinstance(n, ast.Return))
        } - found
        if not new:
            break
        found |= new
    contexts = {
        fn.name
        for fn in functions
        if _is_context_manager(fn) and any(returns_client(n.value) for n in _own_nodes(fn) if isinstance(n, ast.Yield))
    }
    return found, contexts


class TestFactoryCompleteness:
    def test_factories_match_connection_manager(self) -> None:
        functions, contexts = _client_factories((SRC / "connection_manager.py").read_text(encoding="utf-8"))
        assert functions == set(FACTORIES)
        assert contexts == set(CONTEXT_FACTORIES)

    def test_detection_on_a_synthetic_module(self) -> None:
        source = textwrap.dedent(
            """
            import contextlib

            class M:
                async def _open(cls):
                    client = build()
                    return client

                async def get(cls):
                    return await cls._open()

                async def flag(cls):
                    return True

                async def __aenter__(self):
                    return await self.get()

                @contextlib.asynccontextmanager
                async def pair(cls):
                    yield (await cls.get(), False)

                @contextlib.asynccontextmanager
                async def other(cls):
                    yield tx
            """
        )
        assert _client_factories(source) == ({"_open", "get"}, {"pair"})


# --- (c) the real tree ------------------------------------------------------------------------------


def test_the_orm_calls_the_sdk_only_through_the_frontier() -> None:
    violations: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "_client.py" and path.parent == SRC:
            continue
        violations += frontier_violations(path.read_text(encoding="utf-8"), str(path.relative_to(SRC.parent.parent)))
    assert violations == [], "\n".join(violations)


# --- the runtime check (tests/_frontier_spy.py) -----------------------------------------------------


class _FakeConnection:
    """Stands in for an SDK connection class."""

    def query(self, sql: str) -> str:
        return f"ran {sql}"

    def select(self, target: str) -> str:
        return f"selected {target}"


def _call_from(module_name: str, client: _FakeConnection) -> str:
    """Call ``client.query()`` from code whose module is *module_name* (a fake caller module)."""
    namespace: dict[str, Any] = {"__name__": module_name}
    exec(compile("def run(client):\n    return client.query('RETURN 1')\n", f"<{module_name}>", "exec"), namespace)
    return str(namespace["run"](client))


class TestRuntimeSpy:
    @pytest.fixture
    def spy(self) -> Iterator[_frontier_spy.FrontierSpy]:
        spy = _frontier_spy.FrontierSpy()
        spy.instrument([_FakeConnection, None])
        yield spy
        spy.restore()

    def test_an_orm_module_outside_the_frontier_is_recorded(self, spy: _frontier_spy.FrontierSpy) -> None:
        assert _call_from("surreal_orm_lite.model_base", _FakeConnection()) == "ran RETURN 1"
        [line] = spy.violations
        assert line.startswith("surreal_orm_lite.model_base.run (<surreal_orm_lite.model_base>:2) ")
        assert line.endswith("called _FakeConnection.query()")

    @pytest.mark.parametrize("module", ["surreal_orm_lite._client", "tests.test_e2e", "app.models", "surreal_orm_lite_extra"])
    def test_the_frontier_and_other_code_are_allowed(self, spy: _frontier_spy.FrontierSpy, module: str) -> None:
        _call_from(module, _FakeConnection())
        assert spy.violations == {}

    def test_a_repeated_violation_is_listed_once(self, spy: _frontier_spy.FrontierSpy) -> None:
        for _ in range(3):
            _call_from("surreal_orm_lite.edge", _FakeConnection())
        assert len(spy.violations) == 1

    def test_restore_puts_the_original_methods_back(self) -> None:
        original = _FakeConnection.__dict__["select"]
        spy = _frontier_spy.FrontierSpy()
        spy.instrument([_FakeConnection])
        assert _FakeConnection.__dict__["select"] is not original
        assert _FakeConnection.__dict__["select"].__wrapped__ is original
        spy.restore()
        assert _FakeConnection.__dict__["select"] is original

    @pytest.mark.parametrize(
        ("value", "mode"),
        [("", "enforce"), ("enforce", "enforce"), (" Report ", "report"), ("off", "off"), ("bogus", "enforce")],
    )
    def test_mode_from_env(self, value: str, mode: str) -> None:
        assert _frontier_spy.mode_from_env({_frontier_spy.ENV: value}) == mode

    def test_the_session_spy_watches_the_sdk_classes(self, pytestconfig: pytest.Config) -> None:
        plugin = pytestconfig.pluginmanager.get_plugin("orm_frontier_spy")
        assert isinstance(plugin, _frontier_spy.FrontierSpyPlugin)
        if plugin.mode == "off":
            pytest.skip(f"{_frontier_spy.ENV}=off")
        for cls in (_sdk.SdkAsyncWsConnection, _sdk.SdkAsyncHttpConnection, _sdk.SdkAsyncEmbeddedConnection):
            if cls is not None:
                # the embedded class inherits the WebSocket class's methods, and so their wrappers
                assert all(hasattr(getattr(cls, name), "__wrapped__") for name in _frontier_spy.METHODS), cls

    def test_enforce_fails_the_session_and_report_does_not(self) -> None:
        class _Session:
            exitstatus: int = pytest.ExitCode.OK

        for mode, expected in (("enforce", pytest.ExitCode.TESTS_FAILED), ("report", pytest.ExitCode.OK)):
            plugin = _frontier_spy.FrontierSpyPlugin(mode)
            plugin.spy.violations["x"] = None
            session = _Session()
            plugin.pytest_sessionfinish(session, pytest.ExitCode.OK)  # type: ignore[arg-type]
            assert session.exitstatus == expected

    def test_terminal_summary_lists_the_violations(self) -> None:
        lines: list[str] = []

        class _Reporter:
            def section(self, title: str, sep: str) -> None:
                lines.append(title)

            def line(self, text: str) -> None:
                lines.append(text)

        plugin = _frontier_spy.FrontierSpyPlugin("report")
        plugin.pytest_terminal_summary(_Reporter())
        assert lines == []
        plugin.spy.violations["surreal_orm_lite.edge.f (x.py:1) called C.query()"] = None
        plugin.pytest_terminal_summary(_Reporter())
        assert lines == [
            "SDK calls outside surreal_orm_lite._client (ORM_FRONTIER_SPY=report: not failing the session)",
            "surreal_orm_lite.edge.f (x.py:1) called C.query()",
        ]


# --- tests/_sdk_calls.py: request counting on the frontier ------------------------------------------------


class _Builder:
    """An SDK 3.0-style builder: awaitable, with ``merge()``/``patch()`` refinements."""

    def __init__(self, result: Any) -> None:
        self.result = result

    def merge(self, data: Any) -> _Builder:
        return _Builder({"merged": data})

    def patch(self, ops: Any) -> _Builder:
        return _Builder({"patched": ops})

    def __await__(self) -> Any:
        async def answer() -> Any:
            return self.result

        return answer().__await__()


class _FakeClient:
    """Answers each data method with a builder, as SDK 3.0's clients do; never an SDK class (the spy ignores it)."""

    def query(self, sql: str, variables: Any = None) -> _Builder:
        return _Builder([[{"sql": sql}], [{"second": True}]])

    def query_raw(self, sql: str, variables: Any = None) -> _Builder:
        return _Builder({"result": [{"status": "OK", "result": sql}]})

    def select(self, target: Any) -> _Builder:
        return _Builder({"id": target})

    def create(self, target: Any, data: Any) -> _Builder:
        return _Builder(data)

    def update(self, target: Any, data: Any = None) -> _Builder:
        return _Builder(data)


class TestSdkCalls:
    """``tests/_sdk_calls.py``: what ``record()`` and ``hold_next()`` see of the frontier, client by client."""

    async def test_record_sees_each_call_of_its_client_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mine, other = _FakeClient(), _FakeClient()
        calls = _sdk_calls.record(monkeypatch, mine, "query", "query_all", "select", "create", "merge")
        assert await _client.query(mine, "RETURN $a;", {"a": 1}) == [{"sql": "RETURN $a;"}]
        assert len(await _client.query_all(mine, "SELECT * FROM t;")) == 2
        await _client.select(mine, RecordID("t", 1))
        await _client.create(mine, "t", {"v": 1})
        assert await _client.merge(mine, RecordID("t", 1), {"v": 2}) == {"merged": {"v": 2}}
        await _client.query(other, "RETURN 2;")
        await _client.select(other, "t")
        assert calls == [
            _sdk_calls.Call("query", "RETURN $a;", {"a": 1}),
            _sdk_calls.Call("query_all", "SELECT * FROM t;", {}),
            _sdk_calls.Call("select", RecordID("t", 1), None),
            _sdk_calls.Call("create", "t", None),
            _sdk_calls.Call("merge", RecordID("t", 1), None),
        ]

    async def test_record_ignores_the_functions_it_was_not_given(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeClient()
        calls = _sdk_calls.record(monkeypatch, client, "select")
        await _client.query(client, "RETURN 1;")
        await _client.query_raw(client, "RETURN 1;")
        assert calls == []

    async def test_record_keeps_what_entry_gives(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeClient()
        calls = _sdk_calls.record(monkeypatch, client, "query", entry=lambda call: (call.target, call.variables))
        await _client.query(client, sql="RETURN $x;", variables={"x": 1})
        assert calls == [("RETURN $x;", {"x": 1})]

    async def test_record_keeps_a_call_that_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeClient()

        def refused(sql: str, variables: Any = None) -> Any:
            raise RuntimeError("refused")

        monkeypatch.setattr(client, "query", refused)
        calls = _sdk_calls.record(monkeypatch, client, "query", entry=lambda call: call.target)
        with pytest.raises(RuntimeError, match="refused"):
            await _client.query(client, "RETURN 1;")
        assert calls == ["RETURN 1;"]

    async def test_two_clients_stack_on_one_function(self, monkeypatch: pytest.MonkeyPatch) -> None:
        first, second = _FakeClient(), _FakeClient()
        a = _sdk_calls.record(monkeypatch, first, "query", entry=lambda call: call.target)
        b = _sdk_calls.record(monkeypatch, second, "query", entry=lambda call: call.target)
        await _client.query(first, "RETURN 1;")
        await _client.query(second, "RETURN 2;")
        assert (a, b) == (["RETURN 1;"], ["RETURN 2;"])

    async def test_the_patches_end_with_monkeypatch(self) -> None:
        real = _client.query
        with pytest.MonkeyPatch.context() as patched:
            _sdk_calls.record(patched, _FakeClient(), "query")
            assert _client.query is not real
        assert _client.query is real

    def test_names_are_checked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeClient()
        with pytest.raises(ValueError, match="'version' is not a request function"):
            _sdk_calls.record(monkeypatch, client, "version")
        with pytest.raises(ValueError, match="at least one"):
            _sdk_calls.record(monkeypatch, client)
        with pytest.raises(ValueError, match="'normalize' is not a request function"):
            _sdk_calls.hold_next(monkeypatch, client, "normalize")
        with pytest.raises(ValueError, match="at least one"):
            _sdk_calls.hold_next(monkeypatch, client)

    async def test_hold_next_holds_one_call_after_its_reply(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client, other = _FakeClient(), _FakeClient()
        reached, release = _sdk_calls.hold_next(monkeypatch, client, "query", "select")
        assert await _client.query(other, "RETURN 0;") == [{"sql": "RETURN 0;"}]  # another client: not held
        assert not reached.is_set()
        held = asyncio.ensure_future(_client.query(client, "RETURN 1;"))
        await asyncio.wait_for(reached.wait(), timeout=5)
        assert not held.done()
        # Disarmed: the calls made meanwhile pass through, on either function.
        assert await _client.select(client, RecordID("t", 1)) == {"id": RecordID("t", 1)}
        assert await _client.query(client, "RETURN 2;") == [{"sql": "RETURN 2;"}]
        release.set()
        assert await asyncio.wait_for(held, timeout=5) == [{"sql": "RETURN 1;"}]

    async def test_hold_next_and_record_stack(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeClient()
        calls = _sdk_calls.record(monkeypatch, client, "query", entry=lambda call: call.target)
        reached, release = _sdk_calls.hold_next(monkeypatch, client, "query")
        held = asyncio.ensure_future(_client.query(client, "RETURN 1;"))
        await asyncio.wait_for(reached.wait(), timeout=5)
        assert calls == ["RETURN 1;"]
        release.set()
        await held
