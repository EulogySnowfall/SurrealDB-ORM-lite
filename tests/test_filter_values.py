"""Filter values: a ``str`` is data, whatever it starts with; ``Var("name")`` is the only variable reference.

``filter(token="$x")`` compares ``token`` with the string ``"$x"``, bound like any other value, on every filter
path: ``filter()``, ``Q`` (``|``, ``&``, ``~``), ``get()``, ``count()``, ``exists()``, ``get_or_create()``,
``update_or_create()``, ``bulk_update()`` by filter, ``cache()``, a subquery and a live query (the value written as
the string literal ``'$x'``). ``filter(token=Var("x"))`` reads the variable ``$x`` given to ``variables()``.

The ``"$$"`` escape of v0.14.3 keeps its meaning for one more cycle: a scalar value starting with ``"$$"`` is the
same value with one ``$`` less (``"$$x"`` is the string ``"$x"``), with a ``DeprecationWarning`` whose text is the
same whatever the value. A scalar value that is exactly ``$`` and the name of a variable the statement's
``variables()`` give — ``filter(age__lte="$max_age").variables(max_age=25)``, which read ``$max_age`` until v0.28.1
— is still the string, with a ``UserWarning`` naming ``Var("max_age")``. So is a scalar value that is exactly ``$``
and the name of one of SurrealDB's session parameters (``$access``, ``$auth``, ``$session``, ``$token``): ``"$auth"``
is the string, with a ``UserWarning`` naming ``Var("auth")``, which reads the parameter. Each warning is given once
per read, at the caller's line, ``cache()`` and a re-sent statement included. On an edge's ``in``/``out`` such a
value raises ``ValueError`` (it is not a record id) naming ``Var(...)``. As a criterion of ``get_or_create()``/
``update_or_create()``, whatever its lookup, such a value raises ``SurrealDbError`` before any request, saying to pass
the actual value. ``variables()`` refuses the session parameters' names, saying how to pass one's own value under
another name and that ``Var("token")`` alone reads SurrealDB's ``$token``. Unit tests first (no engine, no server); the ``E2E`` classes run on the embedded ``mem://`` engine and on the server of
``SURREALDB_PORT`` (both lines in CI), the live query on the server only.
"""

import asyncio
import contextlib
import warnings
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any
from uuid import uuid4

import pytest
from surrealdb import RecordID

from surreal_orm_lite import (
    BaseSurrealEdge,
    BaseSurrealModel,
    Count,
    Q,
    QueryCache,
    Subquery,
    SurrealConfigDict,
    SurrealDBConnectionManager,
    Var,
)
from surreal_orm_lite.constants import LOOKUP_OPERATORS
from surreal_orm_lite.exceptions import SurrealDbError
from surreal_orm_lite.testing import embedded_database
from surreal_orm_lite.utils import build_filter_condition
from tests.conftest import orm_client
from tests.test_embedded import _normalised, _readme_part, _subsection, _table_rows
from tests.test_select import _SELECT_ROW
from tests.test_time_travel import _SERVER_BEHAVIOUR


class FvItem(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="fv_item")

    id: str | None = None
    token: str | None = None
    name: str = ""


class FvNote(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="fv_note")

    id: str | None = None
    item: Any = None


class FvLink(BaseSurrealEdge):
    model_config = SurrealConfigDict(table_name="fv_link")


class FvDoc(BaseSurrealModel):
    """A record with an owner, read by a record user (``$auth``)."""

    model_config = SurrealConfigDict(table_name="fv_doc")

    id: str | None = None
    owner: Any = None
    title: str = ""


class FvGhost(BaseSurrealModel):
    """A table no test creates: a subquery on it is re-sent by SurrealDB 3.x (it reads as empty)."""

    model_config = SurrealConfigDict(table_name="fv_ghost")

    id: str | None = None
    name: str = ""


_TABLES = ("fv_item", "fv_note", "fv_ghost")

#: The scalar lookups, each with its operator: a ``str`` value is bound as ``$_f0``.
_SCALAR_LOOKUPS = [
    (lookup, op)
    for lookup, op in LOOKUP_OPERATORS.items()
    if lookup not in ("isnull", "in", "not_in", "containsall", "containsany")
]

#: Strings that start with ``$``: each is data. ``$auth`` names a session parameter, ``$this`` and ``$parent``
#: statement parameters, ``$_f0`` the ORM's own first bound value.
_DOLLAR_VALUES = ["$x", "$min_age", "$auth", "$this", "$parent", "$_f0", "$", "$1", "$x y"]


async def _forbidden_client(*args: Any, **kwargs: Any) -> Any:
    pytest.fail("a client was acquired")


@pytest.fixture
def _no_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SurrealDBConnectionManager, "get_client", _forbidden_client)


@pytest.fixture
def _no_warning() -> Iterator[None]:
    """Any warning raised by the test body is an error: a ``str`` value that is data warns of nothing."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        # A socket another test left open can be finalised by the garbage collector during this one: its
        # ResourceWarning is not this test's, and pytest reports such finaliser errors while this filter holds.
        warnings.simplefilter("ignore", ResourceWarning)
        warnings.simplefilter("default", pytest.PytestUnraisableExceptionWarning)
        yield


@pytest.fixture(autouse=True)
def _fresh_state() -> Iterator[None]:
    from surreal_orm_lite import live

    QueryCache.configure(default_ttl=60, max_size=1000, enabled=True)
    QueryCache.clear()
    yield
    QueryCache.clear()
    live._SUBSCRIBERS.clear()
    live._RECENTLY_KILLED.clear()


#: The ``"$$"`` escape's ``DeprecationWarning``: the same text whatever the value.
_ESCAPE_WARNING = (
    "A filter value starting with '$$' is read with one '$' less: the '$$' escape is deprecated. A string filter "
    "value is always data, so write it with one '$' less; Var('name') references a query variable."
)


#: SurrealDB's session parameters: the server sets them for every session and refuses ``LET`` on them
#: (measured on 2.7.0 and 3.3.4). A ``str`` filter value ``"$<name>"`` for one of them warns.
_SESSION_PARAMETERS = ["access", "auth", "session", "token"]

#: ``$``-names SurrealDB gives no session value (statement parameters, a 1.x name, case variants): data, no warning.
_OTHER_NAMES = ["$foo", "$this", "$parent", "$scope", "$value", "$before", "$after", "$input", "$event", "$AUTH"]


def _parameter_warning(name: str) -> str:
    """The ``UserWarning`` of a ``str`` value that is ``$`` and the name *name* of a SurrealDB session parameter."""
    return (
        f"The filter value '${name}' is compared as the string '${name}', not with SurrealDB's ${name} parameter; "
        f"to read the parameter, use Var('{name}')."
    )


def _variable_warning(name: str) -> str:
    """The ``UserWarning`` of a ``str`` value that is ``$`` and the name *name* of a variable of ``variables()``."""
    return (
        f"The filter value '${name}' is compared as the string '${name}', not with the variable {name} given to "
        f"variables(); to reference the variable, use Var('{name}')."
    )


@contextlib.contextmanager
def _recorded() -> Iterator[list[warnings.WarningMessage]]:
    """Every warning raised in the block, none filtered out."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        yield caught


def _messages(caught: list[warnings.WarningMessage], category: type[Warning] = UserWarning) -> list[str]:
    return [str(warning.message) for warning in caught if warning.category is category]


@pytest.mark.usefixtures("_no_client", "_no_warning")
class TestStringsAreData:
    """A ``str`` value is bound as data on every filter path, and nothing warns."""

    @pytest.mark.parametrize("value", _DOLLAR_VALUES)
    @pytest.mark.parametrize(("lookup", "op"), _SCALAR_LOOKUPS, ids=[lookup for lookup, _ in _SCALAR_LOOKUPS])
    def test_every_scalar_lookup_binds_the_string(self, lookup: str, op: str, value: str) -> None:
        assert build_filter_condition("token", lookup, value, 0) == (f"token {op} $_f0", {"_f0": value}, 1)

    def test_filter(self) -> None:
        assert FvItem.objects().filter(token="$x")._compile_query() == (
            "SELECT * FROM fv_item WHERE token = $_f0;",
            {"_f0": "$x"},
        )

    def test_q(self) -> None:
        query, variables = FvItem.objects().filter(Q(token="$x") | ~Q(name="$y"), name__startswith="$")._compile_query()
        assert query == (
            "SELECT * FROM fv_item WHERE ((name).is_string() AND string::starts_with(name, $_f0)) "
            "AND (token = $_f1 OR NOT (name = $_f2));"
        )
        assert variables == {"_f0": "$", "_f1": "$x", "_f2": "$y"}

    def test_a_list_lookup(self) -> None:
        assert FvItem.objects().filter(token__in=["$x", "$$y"])._compile_query() == (
            "SELECT * FROM fv_item WHERE token IN $_f0;",
            {"_f0": ["$x", "$$y"]},
        )

    def test_the_record_id_column(self) -> None:
        assert FvItem.objects().filter(id="$x")._compile_query() == (
            "SELECT * FROM fv_item WHERE id = $_f0;",
            {"_f0": RecordID("fv_item", "$x")},
        )

    def test_an_edge_endpoint_column_wants_a_record_id(self) -> None:
        with pytest.raises(ValueError, match="Invalid record identifier") as raised:
            FvLink.objects().filter(in_="$x")._compile_query()
        assert "Var(" not in str(raised.value)

    def test_a_subquery(self) -> None:
        query, variables = FvNote.objects().filter(item__in=FvItem.objects().filter(token="$x"))._compile_query()
        assert query == (
            "{ LET $_sq0 = (SELECT VALUE id FROM fv_item WHERE token = $_f0); (SELECT * FROM fv_note WHERE item IN $_sq0) };"
        )
        assert variables == {"_f0": "$x"}

    @pytest.mark.parametrize("value", ["$x", "$this"])
    def test_a_live_query_writes_the_string(self, value: str) -> None:
        assert (
            FvItem.objects().filter(token=value)._compile_live(False) == f"LIVE SELECT * FROM fv_item WHERE token = '{value}';"
        )

    def test_the_get_or_create_payload(self) -> None:
        assert FvItem.objects()._criteria_payload({"token": "$x", "name__startswith": "$"}) == {"token": "$x"}


@pytest.mark.usefixtures("_no_client", "_no_warning")
class TestVarIsTheReference:
    """``Var("name")`` reads the query variable ``$name``; nothing else does."""

    def test_filter(self) -> None:
        assert FvItem.objects().filter(token=Var("x")).variables(x="$x")._compile_query() == (
            "SELECT * FROM fv_item WHERE token = $x;",
            {"x": "$x"},
        )

    def test_a_live_query_inlines_the_variable(self) -> None:
        live = FvItem.objects().filter(token=Var("x")).variables(x="$y")._compile_live(False)
        assert live == "LIVE SELECT * FROM fv_item WHERE token = '$y';"

    def test_cannot_be_written_by_get_or_create(self) -> None:
        from surreal_orm_lite.exceptions import SurrealDbError

        with pytest.raises(SurrealDbError, match="query-variable reference"):
            FvItem.objects()._criteria_payload({"token": Var("x")})


@pytest.mark.usefixtures("_no_client")
class TestDollarEscape:
    """``"$$x"`` is still the string ``"$x"`` as a scalar value, with a ``DeprecationWarning``."""

    @pytest.mark.parametrize(("value", "data"), [("$$x", "$x"), ("$$", "$"), ("$$$x", "$$x"), ("$$x y", "$x y")])
    def test_one_dollar_less(self, value: str, data: str) -> None:
        with pytest.warns(DeprecationWarning) as caught:
            assert build_filter_condition("token", "exact", value, 0) == ("token = $_f0", {"_f0": data}, 1)
        assert [str(warning.message) for warning in caught] == [_ESCAPE_WARNING]

    def test_the_warning_points_at_the_caller(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            FvItem.objects().filter(token="$$x")._build_where()
        [warning] = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert warning.filename == __file__

    def test_the_record_id_column(self) -> None:
        with pytest.warns(DeprecationWarning):
            assert FvItem.objects().filter(id="$$x")._compile_query()[1] == {"_f0": RecordID("fv_item", "$x")}

    @pytest.mark.usefixtures("_no_warning")
    def test_the_get_or_create_payload_matches_the_lookup(self) -> None:
        """The record ``get_or_create(token="$$x")`` creates is the one its lookup finds: ``token = "$x"``."""
        assert FvItem.objects()._criteria_payload({"token": "$$x"}) == {"token": "$x"}

    @pytest.mark.usefixtures("_no_warning")
    def test_a_list_item_is_not_unescaped(self) -> None:
        assert build_filter_condition("token", "in", ["$$x"], 0) == ("token IN $_f0", {"_f0": ["$$x"]}, 1)


#: Each statement a queryset compiles, for ``filter(token="$x")`` (its subquery, for the last three), with
#: ``variables(x=…)`` on the outer queryset, the inner one, or an inner one compiled after the outer filter.
_STATEMENTS: dict[str, Callable[[], Any]] = {
    "select": lambda: FvItem.objects().filter(token="$x").variables(x=1)._compile_query(),
    "count": lambda: FvItem.objects().filter(token="$x").variables(x=1)._compile_aggregation_query("count()"),
    "values-annotate": lambda: (
        FvItem.objects().filter(token="$x").variables(x=1).values("name").annotate(n=Count())._compile_group_by_query()
    ),
    "live": lambda: FvItem.objects().filter(token="$x").variables(x=1)._compile_live(False),
    "subquery, outer variables": lambda: (
        FvNote.objects().filter(item__in=FvItem.objects().filter(token="$x")).variables(x=1)._compile_query()
    ),
    "subquery, its own variables": lambda: (
        FvNote.objects().filter(item__in=FvItem.objects().filter(token="$x").variables(x=1))._compile_query()
    ),
    "outer filter, a later subquery's variables": lambda: (
        FvNote.objects().filter(item="$x").filter(item__in=Subquery(FvItem.objects().variables(x=1)))._compile_query()
    ),
}


@pytest.mark.usefixtures("_no_client")
class TestVariableNameWarning:
    """A ``str`` value that is ``$`` and a name ``variables()`` gives is still data, with a ``UserWarning``."""

    def test_filter(self) -> None:
        """The value stays bound as data; the warning names ``Var("x")``, once, at the caller's line."""
        with _recorded() as caught:
            query, variables = FvItem.objects().filter(token="$x").variables(x="other")._compile_query()
        assert query == "SELECT * FROM fv_item WHERE token = $_f0;"
        assert variables == {"x": "other", "_f0": "$x"}
        assert [(warning.category, str(warning.message)) for warning in caught] == [(UserWarning, _variable_warning("x"))]
        assert caught[0].filename == __file__

    @pytest.mark.parametrize(("lookup", "op"), _SCALAR_LOOKUPS, ids=[lookup for lookup, _ in _SCALAR_LOOKUPS])
    def test_every_scalar_lookup(self, lookup: str, op: str) -> None:
        """``lte``/``lt`` would otherwise match every number silently: a number sorts below a string."""
        with _recorded() as caught:
            qs = FvItem.objects().filter(**{f"token__{lookup}": "$max_age"}).variables(max_age=25)
            assert qs._compile_query() == (
                f"SELECT * FROM fv_item WHERE token {op} $_f0;",
                {"max_age": 25, "_f0": "$max_age"},
            )
        assert _messages(caught) == [_variable_warning("max_age")]

    def test_q(self) -> None:
        with _recorded() as caught:
            qs = FvItem.objects().filter(Q(token="$x") | ~Q(name="$y")).variables(x=1, y=2)
            assert qs._compile_query()[1] == {"x": 1, "y": 2, "_f0": "$x", "_f1": "$y"}
        assert _messages(caught) == [_variable_warning("x"), _variable_warning("y")]

    @pytest.mark.parametrize("statement", list(_STATEMENTS))
    def test_every_statement(self, statement: str) -> None:
        with _recorded() as caught:
            _STATEMENTS[statement]()
        assert _messages(caught) == [_variable_warning("x")]
        assert caught[0].filename == __file__

    def test_a_live_query_writes_the_string(self) -> None:
        with _recorded() as caught:
            live = FvItem.objects().filter(token="$x").variables(x="y")._compile_live(False)
        assert live == "LIVE SELECT * FROM fv_item WHERE token = '$x';"
        assert _messages(caught) == [_variable_warning("x")]

    def test_once_per_name_and_statement(self) -> None:
        with _recorded() as caught:
            FvItem.objects().filter(token="$x", name="$x").filter(Q(token__lte="$x")).variables(x=1)._compile_query()
        assert _messages(caught) == [_variable_warning("x")]

    def test_the_escape_warns_of_the_escape_only(self) -> None:
        """``"$$x"`` was the string ``"$x"`` before v0.28.1 too: nothing changed, the escape's warning is the only one."""
        with _recorded() as caught:
            FvItem.objects().filter(token="$$x").variables(x=1)._compile_query()
        assert [(warning.category, str(warning.message)) for warning in caught] == [(DeprecationWarning, _ESCAPE_WARNING)]

    @pytest.mark.usefixtures("_no_warning")
    def test_a_name_variables_does_not_give(self) -> None:
        """Data such as the string ``"$x"``, with no variable ``x``: nothing warns."""
        FvItem.objects().filter(token="$x")._compile_query()
        FvItem.objects().filter(token="$x").variables(y=1)._compile_query()
        FvNote.objects().filter(item__in=FvItem.objects().filter(token="$x").variables(y=1))._compile_query()

    @pytest.mark.usefixtures("_no_warning")
    @pytest.mark.parametrize("value", ["$x y", "$x.y", "$x\n", " $x", "$", "$1", "x"])
    def test_a_value_that_is_not_a_variable_name(self, value: str) -> None:
        FvItem.objects().filter(token=value).variables(**{"x": 1, "1": 2, "x y": 3})._compile_query()

    @pytest.mark.usefixtures("_no_warning")
    def test_an_item_of_a_list_and_var(self) -> None:
        """An item of a list lookup was never a reference; ``Var("x")`` is the reference."""
        FvItem.objects().filter(token__in=["$x"], name__not_in=["$x"]).variables(x=1)._compile_query()
        FvItem.objects().filter(token=Var("x")).variables(x=1)._compile_query()


#: Each statement a queryset compiles, for ``filter(token="$auth")`` (its subquery, for the last one).
_PARAMETER_STATEMENTS: dict[str, Callable[[], Any]] = {
    "select": lambda: FvItem.objects().filter(token="$auth")._compile_query(),
    "count": lambda: FvItem.objects().filter(token="$auth")._compile_aggregation_query("count()"),
    "values-annotate": lambda: (
        FvItem.objects().filter(token="$auth").values("name").annotate(n=Count())._compile_group_by_query()
    ),
    "live": lambda: FvItem.objects().filter(token="$auth")._compile_live(False),
    "subquery": lambda: FvNote.objects().filter(item__in=FvItem.objects().filter(token="$auth"))._compile_query(),
}

#: Compiled in a namespace of its own, so its ``__warningregistry__`` holds this one call site's entries only.
_REGISTRY_PROBE = "def run(value):\n    FvItem.objects().filter(token=value)._compile_query()\n"


@pytest.mark.usefixtures("_no_client")
class TestSessionParameterWarning:
    """A ``str`` value that is ``$`` and the name of a SurrealDB session parameter is data, with a ``UserWarning``."""

    @pytest.mark.parametrize("name", _SESSION_PARAMETERS)
    def test_filter(self, name: str) -> None:
        """The value stays bound as data; the warning names ``Var(name)``, once, at the caller's line."""
        with _recorded() as caught:
            assert FvItem.objects().filter(token=f"${name}")._compile_query() == (
                "SELECT * FROM fv_item WHERE token = $_f0;",
                {"_f0": f"${name}"},
            )
        assert [(warning.category, str(warning.message)) for warning in caught] == [(UserWarning, _parameter_warning(name))]
        assert caught[0].filename == __file__

    @pytest.mark.parametrize("statement", list(_PARAMETER_STATEMENTS))
    def test_every_statement(self, statement: str) -> None:
        with _recorded() as caught:
            _PARAMETER_STATEMENTS[statement]()
        assert _messages(caught) == [_parameter_warning("auth")]
        assert caught[0].filename == __file__

    def test_a_live_query_writes_the_string(self) -> None:
        with _recorded() as caught:
            live = FvItem.objects().filter(token="$auth")._compile_live(False)
        assert live == "LIVE SELECT * FROM fv_item WHERE token = '$auth';"
        assert _messages(caught) == [_parameter_warning("auth")]

    def test_once_per_name_and_statement(self) -> None:
        with _recorded() as caught:
            qs = FvItem.objects().filter(token="$auth", name="$auth").filter(Q(token__lte="$auth") | Q(name="$session"))
            qs._compile_query()
        assert _messages(caught) == [_parameter_warning("auth"), _parameter_warning("session")]

    def test_the_escape_warns_of_the_escape_only(self) -> None:
        """``"$$auth"`` was the string ``"$auth"`` before v0.28.1 too: the escape's warning is the only one."""
        with _recorded() as caught:
            assert FvItem.objects().filter(token="$$auth")._compile_query()[1] == {"_f0": "$auth"}
        assert [(warning.category, str(warning.message)) for warning in caught] == [(DeprecationWarning, _ESCAPE_WARNING)]

    @pytest.mark.usefixtures("_no_warning")
    @pytest.mark.parametrize("value", [*_OTHER_NAMES, "$auth.id", "$authx", "$ auth", "auth"])
    def test_a_name_outside_the_set(self, value: str) -> None:
        """``"$foo"`` with no variable ``foo``, a statement parameter, a 1.x name: data, nothing warns."""
        assert FvItem.objects().filter(token=value)._compile_query()[1] == {"_f0": value}
        FvItem.objects().filter(token=value)._compile_live(False)

    @pytest.mark.usefixtures("_no_warning")
    def test_an_item_of_a_list(self) -> None:
        FvItem.objects().filter(token__in=["$auth"], name__not_in=["$session"])._compile_query()

    def test_the_registry_is_bounded(self) -> None:
        """One call site holds one registry entry per session parameter at most, whatever the other values."""
        namespace: dict[str, Any] = {"FvItem": FvItem}
        exec(compile(_REGISTRY_PROBE, "<fv_registry_probe>", "exec"), namespace)
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("default")
            for round_ in range(3):
                for name in _SESSION_PARAMETERS:
                    namespace["run"](f"${name}")
                for index in range(50):
                    namespace["run"](f"$name_{round_}_{index}")
        registry = {key for key in namespace["__warningregistry__"] if key != "version"}
        assert {text for text, _, _ in registry} == {_parameter_warning(name) for name in _SESSION_PARAMETERS}
        assert len(registry) == len(_SESSION_PARAMETERS)


@pytest.mark.usefixtures("_no_client", "_no_warning")
class TestVarReadsSessionParameters:
    """``Var("auth")`` reads SurrealDB's ``$auth``, in a read and in a live query, without ``variables()``."""

    @pytest.mark.parametrize("name", _SESSION_PARAMETERS)
    def test_read(self, name: str) -> None:
        assert FvItem.objects().filter(token=Var(name))._compile_query() == (
            f"SELECT * FROM fv_item WHERE token = ${name};",
            {},
        )

    @pytest.mark.parametrize("name", _SESSION_PARAMETERS)
    def test_live_query(self, name: str) -> None:
        assert (
            FvItem.objects().filter(token=Var(name))._compile_live(False)
            == f"LIVE SELECT * FROM fv_item WHERE token = ${name};"
        )

    def test_scope_is_no_session_parameter(self) -> None:
        """``$scope`` (a 1.x name) is set by neither line: an unbound ``Var("scope")`` is refused in a live query."""
        with pytest.raises(SurrealDbError, match=r"\$scope is not bound"):
            FvItem.objects().filter(token=Var("scope"))._compile_live(False)
        live = FvItem.objects().filter(token=Var("scope")).variables(scope="s")._compile_live(False)
        assert live == "LIVE SELECT * FROM fv_item WHERE token = 's';"


@pytest.mark.usefixtures("_no_client", "_no_warning")
class TestEndpointHint:
    """On an edge's ``in``/``out`` a ``"$<name>"`` value is not a record id: the ``ValueError`` names ``Var(...)``."""

    @pytest.mark.parametrize("name", _SESSION_PARAMETERS)
    def test_a_session_parameter(self, name: str) -> None:
        with pytest.raises(ValueError, match="Invalid record identifier") as raised:
            FvLink.objects().filter(in_=f"${name}")._compile_query()
        assert str(raised.value).endswith(f"; to read SurrealDB's ${name} parameter, use Var('{name}')")

    def test_a_variable(self) -> None:
        with pytest.raises(ValueError, match="Invalid record identifier") as raised:
            FvLink.objects().filter(out="$p").variables(p=RecordID("person", "a"))._compile_query()
        assert str(raised.value).endswith("; to reference the variable, use Var('p')")

    def test_a_subquery_variable_and_q(self) -> None:
        qs = FvLink.objects().filter(Q(out="$p")).variables(q=1)
        with pytest.raises(ValueError, match=r"use Var\('p'\)"):
            qs.filter(id__in=Subquery(FvLink.objects().variables(p=RecordID("person", "a"))))._compile_query()

    def test_the_get_or_create_lookup(self) -> None:
        """The lookup counts the queryset's ``variables()``, as its warning does."""
        qs = FvLink.objects().variables(p=RecordID("person", "a"))
        with pytest.raises(ValueError, match=r"use Var\('p'\)"):
            asyncio.run(qs.get_or_create(in_="$p", out=RecordID("person", "b")))

    @pytest.mark.parametrize("value", ["$x", "$this", "$auth.id"])
    def test_any_other_value(self, value: str) -> None:
        with pytest.raises(ValueError, match="Invalid record identifier") as raised:
            FvLink.objects().filter(in_=value).variables(y=1)._compile_query()
        assert "Var(" not in str(raised.value)

    def test_a_list_item(self) -> None:
        """An item of ``in`` was never a reference: no hint."""
        with pytest.raises(ValueError, match="Invalid record identifier") as raised:
            FvLink.objects().filter(in___in=["$auth"])._compile_query()
        assert "Var(" not in str(raised.value)


@pytest.mark.usefixtures("_no_client", "_no_warning")
class TestProtectedVariableNames:
    """``variables()`` refuses SurrealDB's session parameters: the server refuses a query variable of that name."""

    @pytest.mark.parametrize("name", _SESSION_PARAMETERS)
    def test_refused(self, name: str) -> None:
        qs = FvItem.objects().variables(x=1)
        with pytest.raises(ValueError) as raised:
            qs.variables(y=2, **{name: 1})
        assert str(raised.value) == _protected_variable(name)
        assert qs._variables == {"x": 1}

    def test_any_other_name(self) -> None:
        """``$scope`` (a 1.x name), ``$this`` and ``$AUTH`` are no session parameter: ordinary variables."""
        qs = FvItem.objects().variables(scope=1, this=2, AUTH=3)
        assert qs._variables == {"scope": 1, "this": 2, "AUTH": 3}

    def test_what_the_refusal_advises(self) -> None:
        """One's own value goes under another name, or straight into the filter; ``Var("token")`` alone is ``$token``.

        A live query writes the value of a variable into the statement: the renamed variable gives ``'x'``, as the
        value itself does, while ``Var("token")`` stays the session parameter the server reads.
        """
        own = FvItem.objects().filter(token=Var("my_token")).variables(my_token="x")._compile_live(False)
        assert own == "LIVE SELECT * FROM fv_item WHERE token = 'x';"
        assert FvItem.objects().filter(token="x")._compile_live(False) == own
        session = FvItem.objects().filter(token=Var("token"))._compile_live(False)
        assert session == "LIVE SELECT * FROM fv_item WHERE token = $token;"


def _protected_variable(name: str) -> str:
    """The ``ValueError`` of ``variables()`` given *name*, one of SurrealDB's session parameters."""
    return (
        f"variables() cannot set '{name}': SurrealDB sets ${name} for each session and refuses a query variable of "
        f"that name. To pass your own value, give it another name (variables(my_{name}=...) with Var('my_{name}')) "
        f"or pass the value itself in the filter; Var('{name}') without variables() reads SurrealDB's ${name} "
        "parameter."
    )


def _criterion_refusal(key: str, name: str, *, variable: bool, written: bool = True) -> str:
    """The ``SurrealDbError`` of a ``get_or_create()``/``update_or_create()`` criterion *key* ``"$<name>"``.

    *written*: the criterion's lookup is ``exact``, which the create writes.
    """
    source = f"the variable {name} given to variables()" if variable else f"SurrealDB's ${name} parameter"
    what = "variable" if variable else "parameter"
    how = "looked up and written" if written else "looked up"
    return (
        f"Cannot look up or create a record from the string '${name}' given for '{key}': it names {source}, but a "
        f"string criterion is data, {how} as the string '${name}'. Pass the actual value as the criterion (the "
        f"record id, or the value itself); Var('{name}') reads the {what} in a filter but cannot be written."
    )


_OR_CREATE = ["get_or_create", "update_or_create"]


@pytest.mark.usefixtures("_no_client", "_no_warning")
@pytest.mark.parametrize("method", _OR_CREATE)
class TestStringReferenceCriteria:
    """A scalar criterion ``"$<name>"`` naming a variable of ``variables()`` or a session parameter is refused.

    It is data, so ``update_or_create(owner="$auth", …)`` would look up and write the string ``"$auth"``: every record
    user would share, and overwrite, the one record whose owner is that string. Under another lookup it would still be
    compared as the string. ``get_or_create()`` and ``update_or_create()`` refuse any such scalar criterion before any
    request, saying to pass the actual value; an edge endpoint keeps the lookup's ``ValueError``.
    """

    @pytest.mark.parametrize("name", _SESSION_PARAMETERS)
    def test_a_session_parameter(self, method: str, name: str) -> None:
        with pytest.raises(SurrealDbError) as raised:
            asyncio.run(getattr(FvDoc.objects(), method)(owner=f"${name}", defaults={"title": "t"}))
        assert str(raised.value) == _criterion_refusal("owner", name, variable=False)

    def test_a_variable(self, method: str) -> None:
        qs = FvItem.objects().variables(x=1)
        with pytest.raises(SurrealDbError) as raised:
            asyncio.run(getattr(qs, method)(name="A", token__exact="$x"))
        assert str(raised.value) == _criterion_refusal("token__exact", "x", variable=True)

    @pytest.mark.parametrize(("lookup", "op"), _SCALAR_LOOKUPS)
    def test_any_scalar_lookup(self, method: str, lookup: str, op: str) -> None:
        """Whatever the lookup, the read would compare with the string ``"$auth"``: refused, written or not."""
        key = f"owner__{lookup}"
        with pytest.raises(SurrealDbError) as raised:
            asyncio.run(getattr(FvDoc.objects(), method)(title="t", **{key: "$auth"}))
        assert str(raised.value) == _criterion_refusal(key, "auth", variable=False, written=lookup == "exact")

    def test_a_variable_under_another_lookup(self, method: str) -> None:
        qs = FvItem.objects().variables(x=1)
        with pytest.raises(SurrealDbError) as raised:
            asyncio.run(getattr(qs, method)(name="A", token__gte="$x"))
        assert str(raised.value) == _criterion_refusal("token__gte", "x", variable=True, written=False)

    def test_a_list_item_is_left_to_the_lookup(self, method: str) -> None:
        """An item of a list lookup is data and never warns: nothing to refuse."""
        FvDoc.objects().variables(x=1)._reject_string_references({"owner__in": ["$auth", "$x"], "title": "$admin"})

    def test_the_record_id_column(self, method: str) -> None:
        with pytest.raises(SurrealDbError) as raised:
            asyncio.run(getattr(FvDoc.objects(), method)(id="$token"))
        assert str(raised.value) == _criterion_refusal("id", "token", variable=False)

    def test_a_text_lookup_on_the_record_id_keeps_its_value_error(self, method: str) -> None:
        with pytest.raises(ValueError, match="cannot be applied to the record id column 'id'"):
            asyncio.run(getattr(FvDoc.objects(), method)(id__contains="$auth"))

    @pytest.mark.parametrize("key", ["in_", "in___gte"])
    def test_an_edge_endpoint_keeps_its_value_error(self, method: str, key: str) -> None:
        """An endpoint holds a record id: the lookup refuses the string, naming ``Var(...)``, as for ``filter()``."""
        with pytest.raises(ValueError, match="Invalid record identifier") as raised:
            asyncio.run(getattr(FvLink.objects(), method)(out=RecordID("person", "b"), **{key: "$auth"}))
        assert str(raised.value).endswith("; to read SurrealDB's $auth parameter, use Var('auth')")


@contextlib.asynccontextmanager
async def values_db(kind: str) -> AsyncIterator[Any]:
    """The ORM on *kind*: a fresh ``mem://`` engine, or the server of ``SURREALDB_PORT`` over WebSocket.

    Seeded with ``a`` (token ``"$x"``), ``b`` (no token: ``NONE``), ``c`` (token ``"x"``) and ``d`` (token
    ``"$y"``), and a note on ``a``.
    """
    context = embedded_database("mem://") if kind == "mem" else orm_client(*_TABLES)
    async with context as client:
        await client.query("CREATE fv_item:a SET token = '$x', name = 'A';")
        await client.query("CREATE fv_item:b SET name = 'B';")
        await client.query("CREATE fv_item:c SET token = 'x', name = 'C';")
        await client.query("CREATE fv_item:d SET token = '$y', name = 'D';")
        await client.query("CREATE fv_note:n SET item = fv_item:a;")
        yield client


def _ids(rows: list[Any]) -> list[str]:
    return sorted(str(row.id) for row in rows)


@pytest.mark.usefixtures("_no_warning")
@pytest.mark.parametrize("kind", ["mem", "ws"])
class TestStringsAreDataE2E:
    """``filter(token="$x")`` finds the record whose token is the string ``"$x"``, never one with no token."""

    async def test_filter(self, kind: str) -> None:
        async with values_db(kind):
            assert _ids(await FvItem.objects().filter(token="$x").exec()) == ["a"]
            assert _ids(await FvItem.objects().filter(token="$x").variables(y="x").exec()) == ["a"]
            assert _ids(await FvItem.objects().filter(token="$nothing").exec()) == []
            assert _ids(await FvItem.objects().filter(token__in=["$x", "$y"]).exec()) == ["a", "d"]

    async def test_q(self, kind: str) -> None:
        async with values_db(kind):
            assert _ids(await FvItem.objects().filter(Q(token="$x") | Q(token="$y")).exec()) == ["a", "d"]
            assert _ids(await FvItem.objects().filter(~Q(token="$x")).exec()) == ["b", "c", "d"]

    async def test_reads(self, kind: str) -> None:
        async with values_db(kind):
            qs = FvItem.objects().filter(token="$x")
            assert (await qs.get()).name == "A"
            assert (await qs.first()).name == "A"
            assert await qs.count() == 1
            assert await qs.exists() is True
            assert _ids(await FvItem.objects().filter(token="$x").cache().exec()) == ["a"]

    async def test_var_reads_the_variable(self, kind: str) -> None:
        async with values_db(kind):
            assert _ids(await FvItem.objects().filter(token=Var("x")).variables(x="x").exec()) == ["c"]
            assert _ids(await FvItem.objects().filter(token=Var("x")).variables(x="$x").exec()) == ["a"]

    async def test_get_or_create(self, kind: str) -> None:
        async with values_db(kind):
            found, created = await FvItem.objects().get_or_create(token="$x")
            assert (found.id, created) == ("a", False)
            made, created = await FvItem.objects().get_or_create(token="$z", defaults={"id": "z", "name": "Z"})
            assert (made.token, created) == ("$z", True)
            again, created = await FvItem.objects().get_or_create(token="$z", defaults={"id": "z", "name": "Z"})
            assert (again.id, created) == ("z", False)

    async def test_update_or_create(self, kind: str) -> None:
        async with values_db(kind):
            updated, created = await FvItem.objects().update_or_create(token="$y", defaults={"name": "D2"})
            assert (updated.name, created) == ("D2", False)
            assert {row.id: row.name for row in await FvItem.objects().all()} == {"a": "A", "b": "B", "c": "C", "d": "D2"}

    async def test_bulk_update_by_filter(self, kind: str) -> None:
        async with values_db(kind):
            await FvItem.objects().filter(token="$x").bulk_update(name="hit")
            assert {row.id: row.name for row in await FvItem.objects().all()} == {"a": "hit", "b": "B", "c": "C", "d": "D"}

    async def test_a_subquery(self, kind: str) -> None:
        async with values_db(kind):
            assert _ids(await FvNote.objects().filter(item__in=FvItem.objects().filter(token="$x")).exec()) == ["n"]
            assert _ids(await FvNote.objects().filter(item__in=Subquery(FvItem.objects().filter(token="$q"))).exec()) == []


@pytest.mark.parametrize("kind", ["mem", "ws"])
class TestDollarEscapeE2E:
    async def test_escaped_value_finds_the_string(self, kind: str) -> None:
        async with values_db(kind):
            with pytest.warns(DeprecationWarning):
                assert _ids(await FvItem.objects().filter(token="$$x").exec()) == ["a"]

    async def test_once_per_cached_read(self, kind: str) -> None:
        """A miss compiles the statement twice (key, then read): the escape still warns once per read."""
        async with values_db(kind):
            for _ in range(2):  # a miss, then a hit
                with _recorded() as caught:
                    assert _ids(await FvItem.objects().filter(token="$$x").cache().exec()) == ["a"]
                assert _messages(caught, DeprecationWarning) == [_ESCAPE_WARNING]

    async def test_get_or_create_converges(self, kind: str) -> None:
        async with values_db(kind):
            with pytest.warns(DeprecationWarning):
                found, created = await FvItem.objects().get_or_create(token="$$x")
            assert (found.id, created) == ("a", False)


@pytest.mark.parametrize("kind", ["mem", "ws"])
class TestVariableNameWarningE2E:
    """Every read and write by filter compares with the string, and warns once, at the caller's line."""

    async def test_reads(self, kind: str) -> None:
        async with values_db(kind):
            qs = FvItem.objects().filter(token="$x").variables(x="x")
            expected = {"exec": ["a"], "get": "A", "first": "A", "count": 1, "exists": True}
            for name, want in expected.items():
                with _recorded() as caught:
                    result = await getattr(qs, name)()
                got = _ids(result) if name == "exec" else result.name if name in ("get", "first") else result
                assert got == want, name
                assert _messages(caught) == [_variable_warning("x")], name
                [warning] = [warning for warning in caught if warning.category is UserWarning]
                assert warning.filename == __file__, name

    async def test_once_per_cached_read(self, kind: str) -> None:
        async with values_db(kind):
            for _ in range(2):  # a miss, then a hit
                with _recorded() as caught:
                    assert _ids(await FvItem.objects().filter(token="$x").variables(x="x").cache().exec()) == ["a"]
                assert _messages(caught) == [_variable_warning("x")]

    async def test_once_when_a_missing_subquery_table_resends(self, kind: str) -> None:
        """SurrealDB 3.x has the statement re-sent with the subquery of the missing table empty: still one warning."""
        async with values_db(kind):
            qs = FvItem.objects().filter(token="$x", name="$$A").filter(name__in=FvGhost.objects().select("name"))
            with _recorded() as caught:
                assert await qs.variables(x="x").exec() == []
            assert _messages(caught) == [_variable_warning("x")]
            assert _messages(caught, DeprecationWarning) == [_ESCAPE_WARNING]

    async def test_get_or_create_and_update_or_create(self, kind: str) -> None:
        """A criterion naming a variable of the queryset's ``variables()`` is refused, whatever its lookup: nothing is
        looked up or written, nothing warns."""
        async with values_db(kind):
            names = {row.id: row.name for row in await FvItem.objects().all()}
            for method in _OR_CREATE:
                with _recorded() as caught, pytest.raises(SurrealDbError) as raised:
                    await getattr(FvItem.objects().variables(x="x"), method)(token="$x", defaults={"name": "new"})
                assert str(raised.value) == _criterion_refusal("token", "x", variable=True)
                with pytest.raises(SurrealDbError) as raised:
                    qs = FvItem.objects().variables(y="x")
                    await getattr(qs, method)(token__gte="$y", name="D", defaults={"name": "D2"})
                assert str(raised.value) == _criterion_refusal("token__gte", "y", variable=True, written=False)
                assert _messages(caught) == []
            assert {row.id: row.name for row in await FvItem.objects().all()} == names

    async def test_writes_by_filter(self, kind: str) -> None:
        async with values_db(kind):
            with _recorded() as caught:
                assert await FvItem.objects().filter(token="$x").variables(x="x").bulk_update(name="hit") == 1
            assert _messages(caught) == [_variable_warning("x")]
            with _recorded() as caught:
                assert await FvItem.objects().filter(token="$y").variables(y="x").bulk_delete() == 1
            assert _messages(caught) == [_variable_warning("y")]
            assert {row.id: row.name for row in await FvItem.objects().all()} == {"a": "hit", "b": "B", "c": "C"}


#: A record access whose users may read and write ``fv_doc``: ``$auth`` is the signed-up ``fv_user`` record.
_SESSION_ACCESS = "fv_acct"
_SESSION_TABLES = ("fv_user", "fv_doc")
_SESSION_DDL = (
    "DEFINE TABLE OVERWRITE fv_user SCHEMALESS PERMISSIONS FULL; "
    "DEFINE TABLE OVERWRITE fv_doc SCHEMALESS PERMISSIONS FULL; "
    f"DEFINE ACCESS OVERWRITE {_SESSION_ACCESS} ON DATABASE TYPE RECORD "
    "SIGNUP (CREATE fv_user SET name = $name) SIGNIN (SELECT * FROM fv_user WHERE name = $name) "
    "DURATION FOR SESSION 1h;"
)


@contextlib.asynccontextmanager
async def session_db(kind: str) -> AsyncIterator[Any]:
    """The ORM on *kind*, signed up as a record user of ``fv_acct``.

    The user owns ``fv_doc:mine``; ``fv_doc:theirs`` is another user's and ``fv_doc:text`` holds the string
    ``"$auth"``. On the server the session signs in as root again before the tables and the access are removed.
    """
    context = embedded_database("mem://") if kind == "mem" else orm_client(*_SESSION_TABLES)
    async with context as client:
        await client.query(_SESSION_DDL)
        try:
            await SurrealDBConnectionManager.signup(access=_SESSION_ACCESS, variables={"name": uuid4().hex})
            await client.query(
                "CREATE fv_doc:mine SET owner = $auth; CREATE fv_doc:theirs SET owner = fv_user:other; "
                "CREATE fv_doc:text SET owner = '$auth';"
            )
            yield client
        finally:
            if kind == "ws":
                with contextlib.suppress(Exception):
                    await client.signin({"username": "root", "password": "root"})
                    await client.use("ns", "db")
                with contextlib.suppress(Exception):
                    await client.query(f"REMOVE ACCESS {_SESSION_ACCESS} ON DATABASE;")


@pytest.mark.parametrize("kind", ["mem", "ws"])
class TestSessionParameterE2E:
    """A record user's ``Var("auth")`` reads its own records; the string ``"$auth"`` is data, with a warning."""

    @pytest.mark.usefixtures("_no_warning")
    async def test_var_reads_the_parameter(self, kind: str) -> None:
        async with session_db(kind):
            assert _ids(await FvDoc.objects().filter(owner=Var("auth")).exec()) == ["mine"]
            assert await FvDoc.objects().filter(owner=Var("auth")).count() == 1
            inner = Subquery(FvDoc.objects().filter(owner=Var("auth")))
            assert _ids(await FvDoc.objects().filter(id__in=inner).exec()) == ["mine"]

    async def test_the_string_is_data(self, kind: str) -> None:
        async with session_db(kind):
            with _recorded() as caught:
                assert _ids(await FvDoc.objects().filter(owner="$auth").exec()) == ["text"]
            assert _messages(caught) == [_parameter_warning("auth")]
            [warning] = [warning for warning in caught if warning.category is UserWarning]
            assert warning.filename == __file__


async def _docs() -> dict[str, tuple[str, str]]:
    """Every ``fv_doc`` record: its owner (as text) and title, by id."""
    return {row.id: (str(row.owner), row.title) for row in await FvDoc.objects().all()}


@pytest.mark.parametrize("kind", ["mem", "ws"])
class TestStringReferenceCriteriaE2E:
    """Two record users: ``"$auth"`` as a criterion is refused, ``Var("auth")`` finds a user's own record."""

    async def test_no_shared_record(self, kind: str) -> None:
        """Neither user looks up nor writes the record whose owner is the string ``"$auth"`` (``fv_doc:text``).

        Refused before the lookup: nothing warns.
        """
        async with session_db(kind):
            before = await _docs()
            assert before["text"] == ("$auth", "")
            with _recorded() as caught:
                for user in ("first", "second"):
                    if user == "second":
                        await SurrealDBConnectionManager.signup(access=_SESSION_ACCESS, variables={"name": uuid4().hex})
                    for method in _OR_CREATE:
                        with pytest.raises(SurrealDbError) as raised:
                            await getattr(FvDoc.objects(), method)(owner="$auth", defaults={"title": user})
                        assert str(raised.value) == _criterion_refusal("owner", "auth", variable=False)
            assert await _docs() == before
            assert _messages(caught) == []

    async def test_another_lookup(self, kind: str) -> None:
        """``owner__contains="$auth"`` would be compared with the string: refused before any request, by both methods."""
        async with session_db(kind):
            before = await _docs()
            with _recorded() as caught:
                for method in _OR_CREATE:
                    with pytest.raises(SurrealDbError) as raised:
                        await getattr(FvDoc.objects(), method)(owner__contains="$auth", title="t")
                    assert str(raised.value) == _criterion_refusal("owner__contains", "auth", variable=False, written=False)
            assert await _docs() == before
            assert _messages(caught) == []

    @pytest.mark.usefixtures("_no_warning")
    async def test_var_finds_the_users_own_record(self, kind: str) -> None:
        """``Var("auth")`` is a lookup: found, the record; not found, no record can be created from it."""
        async with session_db(kind):
            before = await _docs()
            found, created = await FvDoc.objects().get_or_create(owner=Var("auth"))
            assert (found.id, created) == ("mine", False)
            await SurrealDBConnectionManager.signup(access=_SESSION_ACCESS, variables={"name": uuid4().hex})
            for method in _OR_CREATE:
                with pytest.raises(SurrealDbError, match="query-variable reference given for 'owner'"):
                    await getattr(FvDoc.objects(), method)(owner=Var("auth"), defaults={"title": "second"})
            assert await _docs() == before

    @pytest.mark.usefixtures("_no_warning")
    async def test_any_other_name_is_data(self, kind: str) -> None:
        """``"$admin"`` names no variable and no session parameter: looked up and written as the string."""
        async with session_db(kind):
            made, created = await FvDoc.objects().update_or_create(owner="$admin", defaults={"title": "one"})
            assert (made.owner, made.title, created) == ("$admin", "one", True)
            again, created = await FvDoc.objects().update_or_create(owner="$admin", defaults={"title": "two"})
            assert (again.title, created) == ("two", False)
            found, created = await FvDoc.objects().variables(other=1).get_or_create(owner="$admin")
            assert (found.title, created) == ("two", False)
            assert [title for owner, title in (await _docs()).values() if owner == "$admin"] == ["two"]


async def _take(stream: Any, count: int, timeout: float = 10.0) -> list[Any]:
    async def _read() -> list[Any]:
        seen: list[Any] = []
        async for item in stream:
            seen.append(item)
            if len(seen) >= count:
                break
        return seen

    return await asyncio.wait_for(_read(), timeout=timeout)


@pytest.mark.usefixtures("_no_warning")
class TestLiveQueryE2E:
    """A live query (server only: the embedded engine has none) notifies for the string ``"$x"`` only."""

    async def test_watch(self) -> None:
        async with orm_client(*_TABLES) as client:
            await client.query("DEFINE TABLE fv_item SCHEMALESS;", {})
            async with FvItem.objects().filter(token="$x").watch() as stream:
                await client.query("CREATE fv_item:none SET name = 'no token';", {})
                await client.query("CREATE fv_item:other SET token = 'x';", {})
                await client.query("CREATE fv_item:hit SET token = '$x';", {})
                [envelope] = await _take(stream, 1)
                assert envelope["result"]["token"] == "$x"
                assert str(envelope["record"].id) == "hit"

    async def test_live(self) -> None:
        async with orm_client(*_TABLES) as client:
            await client.query("DEFINE TABLE fv_item SCHEMALESS;", {})
            async with FvItem.objects().filter(token="$foo").live() as stream:
                await client.query("CREATE fv_item:none SET name = 'no token';", {})
                await client.query("CREATE fv_item:hit SET token = '$foo';", {})
                [event] = await _take(stream, 1)
                assert (event.instance.id, event.instance.token) == ("hit", "$foo")


class TestSessionParameterLiveE2E:
    """A record user's live query: ``Var("auth")`` notifies for its own records, the string ``"$auth"`` for the string."""

    @pytest.mark.usefixtures("_no_warning")
    async def test_var_reads_the_parameter(self) -> None:
        async with session_db("ws") as client, FvDoc.objects().filter(owner=Var("auth")).live() as stream:
            await client.query("CREATE fv_doc:other SET owner = fv_user:other;", {})
            await client.query("CREATE fv_doc:text2 SET owner = '$auth';", {})
            await client.query("CREATE fv_doc:own SET owner = $auth;", {})
            [event] = await _take(stream, 1)
            assert event.instance.id == "own"

    async def test_the_string_is_data(self) -> None:
        async with session_db("ws") as client:
            with _recorded() as caught:
                live = FvDoc.objects().filter(owner="$auth").live()
            assert _messages(caught) == [_parameter_warning("auth")]
            async with live as stream:
                await client.query("CREATE fv_doc:own SET owner = $auth;", {})
                await client.query("CREATE fv_doc:text2 SET owner = '$auth';", {})
                [event] = await _take(stream, 1)
                assert (event.instance.id, event.instance.owner) == ("text2", "$auth")


@pytest.mark.usefixtures("_no_warning")
class TestSessionParameterNameE2E:
    """``variables()`` refuses ``token``: one's own value goes under another name; ``Var("token")`` alone is ``$token``.

    Server only: the live query (the embedded engine has none) and a signed-in session, whose ``$token`` holds the
    claims of its token on both lines.
    """

    async def test_a_live_query_with_a_renamed_variable(self) -> None:
        async with orm_client(*_TABLES) as client:
            await client.query("DEFINE TABLE fv_item SCHEMALESS;", {})
            live = FvItem.objects().filter(token=Var("my_token")).variables(my_token="x").live()
            async with live as stream:
                await client.query("CREATE fv_item:none SET name = 'no token';", {})
                await client.query("CREATE fv_item:other SET token = 'y';", {})
                await client.query("CREATE fv_item:hit SET token = 'x';", {})
                [event] = await _take(stream, 1)
                assert (event.instance.id, event.instance.token) == ("hit", "x")

    async def test_var_reads_the_session_token(self) -> None:
        """Without ``variables()``, ``Var("token")`` reads the session's ``$token``, in a read and in a live query."""
        async with orm_client("fv_doc") as client:
            await client.query(
                "DEFINE TABLE fv_doc SCHEMALESS; CREATE fv_doc:claims SET owner = $token; CREATE fv_doc:text SET owner = 'x';",
                {},
            )
            assert _ids(await FvDoc.objects().filter(owner=Var("token")).exec()) == ["claims"]
            async with FvDoc.objects().filter(owner=Var("token")).live() as stream:
                await client.query("CREATE fv_doc:text2 SET owner = 'x';", {})
                await client.query("CREATE fv_doc:own SET owner = $token;", {})
                [event] = await _take(stream, 1)
                assert event.instance.id == "own"


#: The row the 2.x/3.x behaviour table gives a filter string value, cell by cell.
_FILTER_VALUE_ROW = [
    '`filter(f="$x")`: a string filter value starting with `$`',
    'same on both lines: compared with the string `"$x"`, bound like any value, in a live query too; `Var("x")` reads '
    'the variable `$x`, `Var("auth")` SurrealDB\'s `$auth`',
    "the same; on an edge's `in`/`out` (record ids) such a string raises `ValueError`, on both lines; `$access`, "
    "`$session`, `$token` read like `$auth`",
    "v0.28.1",
]


class TestFilterValueDocs:
    """Unit: what the README says of string filter values and ``Var()``."""

    def test_section(self) -> None:
        text = _normalised(_subsection("#### String filter values and `Var()`", _readme_part("### Supported Filter Lookups")))
        for needle in (
            'compares `name` with the string `"$admin"`',
            "`Var(...)` is the only way to reference a query variable",
            '`"$$admin"`',
            "`DeprecationWarning`",
            'write `"$admin"`',
            "`UserWarning`",
            '`filter(age__lte="$max_age").variables(max_age=25)`',
            '`Var("max_age")`',
            "once per read",
            "emits a `UserWarning`",
            'filter(owner=Var("auth"))',
            'compares `owner` with the string `"$auth"`',
            '`"$auth"`, `"$access"`, `"$session"` or `"$token"`',
            "On an edge's `in` and `out`",
            'update_or_create(owner="$auth", defaults=…)',
            '`members__contains="$auth"`',
            "raises `SurrealDbError` before any request, whatever its lookup, saying to pass the actual value as the "
            "criterion: the record id, or the value itself",
            "`variables()` refuses these four names with a `ValueError`",
            "To pass your own value, give it another name, or pass the value itself in the filter",
            '`filter(token=Var("device_token")).variables(device_token=t)`',
            '`Var("token")` without `variables()` reads SurrealDB\'s `$token`',
        ):
            assert needle in text, needle
        assert "naming the `Var(...)` and `defaults=`" not in text
        assert "Read them with `Var(...)`, without `variables()`" not in text
        assert "silently matches nothing" not in text
        assert "raises a `UserWarning`" not in text

    def test_live_query_note(self) -> None:
        text = _normalised(_readme_part("### 21. Live queries (real-time notifications)"))
        assert "server parameters such as `$auth` and `$session` are allowed" not in text
        for needle in ('`filter(owner=Var("auth"))` streams the signed-in record user\'s own records', "written as `'$auth'`"):
            assert needle in text, needle

    def test_server_behaviour_row(self) -> None:
        """Right after the ``select("meta.*")`` row of v0.28.1, before the table's last row."""
        rows = [cells for _, cells in _table_rows(_readme_part(_SERVER_BEHAVIOUR))]
        index = rows.index(_FILTER_VALUE_ROW)
        assert rows[index - 1] == _SELECT_ROW, rows[index - 1]
        assert index < len(rows) - 1 and rows[-1][3] == "v0.19.1", rows[-1]
