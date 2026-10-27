"""Migration system (v0.38.0): the statements of the operations — the access statement and ``migrations/render.py``.

The access statement (``TestAccessDdlUnchanged``, ``TestAccessStatement``): ``access_ddl()``'s last statement is built
by ``schema.access_statement()`` from the parts ``AuthenticatedUserMixin._access_parts()`` returns, so a migration
writes the access a model's ``define_access()`` writes. ``access_ddl()`` is unchanged byte for byte: its outputs are
pinned as literals, captured before the extraction (#338's ``SET id = $<key>`` form included).

The reader of the ORM's own statements (``TestReadOwn``): every statement the appliers write — ``table_ddl()``,
``view_ddl()``, an edge's relation statement, ``index_ddl()``, ``event_ddl()``, ``access_ddl()`` (its final ``;``
included) and ``function_ddl()`` — goes through ``render.read_own()`` to the state ``DatabaseIntrospector`` builds from
the server's rendering of it; a statement it cannot read is a bug of the ORM (``RuntimeError``), reported without
quoting the statement.

The render parts: ``plain()`` (``TestPlain``, the creation form of an ``OVERWRITE`` statement), ``removal()``
(``TestRemoval``, names in backticks, any name SurrealDB accepts), the one field writer ``field_statement()``
(``TestFieldStatement``, every clause of a ``FieldState`` in the order the servers render them — ``COMMENT`` before
``PERMISSIONS``, measured on 3.3.4 and 2.7.0 —, read back by ``read_own()``), and the ``FLEXIBLE`` rule of SurrealDB 2.x
and the embedded engine: ``flexible_before_3()`` (``TestFlexibleBefore3``, from a schema state),
``flexible_candidate()`` (``TestFlexibleCandidate``, state-free) and ``flexible_redefinitions()``
(``TestFlexibleRedefinitions``, the columns an ``AlterTable`` making a table ``SCHEMAFULL`` redefines on line 2).

The literal reader (``TestLiteralValue``): ``render.literal_value()`` reads back a ``DEFAULT`` text the ORM can write as
a Python value, ``None`` for any other. The statement check (``TestCheckStatements``): ``render.check_statements()``
counts the top-level statements of a ``RawSQL``/``DataMigration`` text and refuses a transaction or ``USE`` statement,
a comment marker, an unterminated literal and a NUL. Unit tests only (no server).
"""

import dataclasses
import re
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

import pytest
from pydantic import BaseModel, Field

from surreal_orm_lite import (
    Analyzer,
    AuthenticatedUserMixin,
    BaseSurrealEdge,
    BaseSurrealModel,
    Computed,
    Event,
    Index,
    SchemaMode,
    SurrealConfigDict,
    TableType,
    computed,
)
from surreal_orm_lite.migrations import render
from surreal_orm_lite.schema import AccessParts, TableClauses, access_statement, function_ddl, view_statement
from surreal_orm_lite.schema_state import (
    AccessState,
    AnalyzerState,
    EventState,
    FieldState,
    FunctionState,
    IndexState,
    SchemaState,
    TableState,
)

# --- access_ddl(), pinned before the extraction ----------------------------------------------------------------------


class AdPlain(AuthenticatedUserMixin, BaseSurrealModel):
    email: str
    password: str


class AdRefresh(AuthenticatedUserMixin, BaseSurrealModel):
    model_config = SurrealConfigDict(access_name="ad_refresh", with_refresh=True, auth_duration_grant="7d")

    email: str
    password: str


class AdKeyed(AuthenticatedUserMixin, BaseSurrealModel):
    model_config = SurrealConfigDict(access_name="ad_keyed", primary_key="email")

    email: str
    password: str
    name: str = ""


class AdAliased(AuthenticatedUserMixin, BaseSurrealModel):
    model_config = SurrealConfigDict(access_name="ad_aliased")

    id: str | None = None
    email: str = Field(alias="mail")
    password: str = Field(alias="password_hash")


class AdBcrypt(AuthenticatedUserMixin, BaseSurrealModel):
    model_config = SurrealConfigDict(
        access_name="account",
        identifier_field="login",
        password_field="secret",
        auth_algorithm="bcrypt",
        auth_duration_token="5m",
        auth_duration_session="1h",
    )

    login: str
    secret: str


class AdKeyedFull(AuthenticatedUserMixin, BaseSurrealModel):
    model_config = SurrealConfigDict(access_name="ad_keyed_full", primary_key="email", schema_mode=SchemaMode.SCHEMAFULL)

    email: str
    password: str


class AdUserTable(AuthenticatedUserMixin, BaseSurrealModel):
    model_config = SurrealConfigDict(access_name="ad_user", table_type=TableType.USER, comment="Users")

    id: str | None = None
    email: str
    password: str


_PLAIN_ACCESS = (
    "DEFINE ACCESS {clause} AdPlain_access ON DATABASE TYPE RECORD SIGNUP ( CREATE AdPlain SET email = $email, "
    "password = crypto::argon2::generate($password) ) SIGNIN ( SELECT * FROM AdPlain WHERE email = $email AND "
    "crypto::argon2::compare(password, $password) ) DURATION FOR TOKEN 15m, FOR SESSION 12h;"
)
_REFRESH_ACCESS = (
    "DEFINE ACCESS {clause} ad_refresh ON DATABASE TYPE RECORD SIGNUP ( CREATE AdRefresh SET email = $email, "
    "password = crypto::argon2::generate($password) ) SIGNIN ( SELECT * FROM AdRefresh WHERE email = $email AND "
    "crypto::argon2::compare(password, $password) ) WITH REFRESH DURATION FOR TOKEN 15m, FOR SESSION 12h, FOR GRANT 7d;"
)
_KEYED_ACCESS = (
    "DEFINE ACCESS {clause} ad_keyed ON DATABASE TYPE RECORD SIGNUP ( CREATE AdKeyed SET id = $email, email = $email, "
    "password = crypto::argon2::generate($password), name = $name ) SIGNIN ( SELECT * FROM AdKeyed WHERE email = "
    "$email AND crypto::argon2::compare(password, $password) ) DURATION FOR TOKEN 15m, FOR SESSION 12h;"
)
_ALIASED_ACCESS = (
    "DEFINE ACCESS {clause} ad_aliased ON DATABASE TYPE RECORD SIGNUP ( CREATE AdAliased SET mail = $email, "
    "password_hash = crypto::argon2::generate($password) ) SIGNIN ( SELECT * FROM AdAliased WHERE mail = $email AND "
    "crypto::argon2::compare(password_hash, $password) ) DURATION FOR TOKEN 15m, FOR SESSION 12h;"
)
_BCRYPT_ACCESS = (
    "DEFINE ACCESS {clause} account ON DATABASE TYPE RECORD SIGNUP ( CREATE AdBcrypt SET login = $login, "
    "secret = crypto::bcrypt::generate($secret) ) SIGNIN ( SELECT * FROM AdBcrypt WHERE login = $login AND "
    "crypto::bcrypt::compare(secret, $secret) ) DURATION FOR TOKEN 5m, FOR SESSION 1h;"
)
_KEYED_FULL_ACCESS = (
    "DEFINE ACCESS {clause} ad_keyed_full ON DATABASE TYPE RECORD SIGNUP ( CREATE AdKeyedFull SET id = $email, "
    "email = $email, password = crypto::argon2::generate($password) ) SIGNIN ( SELECT * FROM AdKeyedFull WHERE email = "
    "$email AND crypto::argon2::compare(password, $password) ) DURATION FOR TOKEN 15m, FOR SESSION 12h;"
)
_USER_TABLE_ACCESS = (
    "DEFINE ACCESS {clause} ad_user ON DATABASE TYPE RECORD SIGNUP ( CREATE AdUserTable SET email = $email, "
    "password = crypto::argon2::generate($password) ) SIGNIN ( SELECT * FROM AdUserTable WHERE email = $email AND "
    "crypto::argon2::compare(password, $password) ) DURATION FOR TOKEN 15m, FOR SESSION 12h;"
)

#: ``(model, the table statements written with {clause}, the access statement written with {clause})``, captured from
#: ``access_ddl()`` on the branch rebased onto #338, before ``access_statement()`` existed.
_PINS: list[tuple[Any, list[str], str]] = [
    (
        AdPlain,
        ["DEFINE TABLE {clause} AdPlain SCHEMALESS PERMISSIONS FOR select, update WHERE id = $auth.id;"],
        _PLAIN_ACCESS,
    ),
    (
        AdRefresh,
        ["DEFINE TABLE {clause} AdRefresh SCHEMALESS PERMISSIONS FOR select, update WHERE id = $auth.id;"],
        _REFRESH_ACCESS,
    ),
    (
        AdKeyed,
        ["DEFINE TABLE {clause} AdKeyed SCHEMALESS PERMISSIONS FOR select, update WHERE id = $auth.id;"],
        _KEYED_ACCESS,
    ),
    (
        AdAliased,
        ["DEFINE TABLE {clause} AdAliased SCHEMALESS PERMISSIONS FOR select, update WHERE id = $auth.id;"],
        _ALIASED_ACCESS,
    ),
    (
        AdBcrypt,
        ["DEFINE TABLE {clause} AdBcrypt SCHEMALESS PERMISSIONS FOR select, update WHERE id = $auth.id;"],
        _BCRYPT_ACCESS,
    ),
    (
        AdKeyedFull,
        [
            "DEFINE TABLE {clause} `AdKeyedFull` TYPE NORMAL SCHEMAFULL PERMISSIONS FOR select, update WHERE id = "
            "$auth.id, FOR create, delete NONE;",
            "DEFINE FIELD {clause} `email` ON `AdKeyedFull` TYPE string;",
            "DEFINE FIELD {clause} `password` ON `AdKeyedFull` TYPE string;",
        ],
        _KEYED_FULL_ACCESS,
    ),
    (
        AdUserTable,
        [
            "DEFINE TABLE {clause} `AdUserTable` TYPE NORMAL SCHEMAFULL COMMENT 'Users' PERMISSIONS FOR select, update "
            "WHERE id = $auth.id, FOR create, delete NONE;",
            "DEFINE FIELD {clause} `email` ON `AdUserTable` TYPE string;",
            "DEFINE FIELD {clause} `password` ON `AdUserTable` TYPE string;",
        ],
        _USER_TABLE_ACCESS,
    ),
]


def _pin_id(pin: tuple[Any, list[str], str]) -> str:
    return str(pin[0].__name__)


class TestAccessDdlUnchanged:
    """AC-24: ``access_ddl()`` through ``schema.access_statement()`` writes #338's text byte for byte."""

    @pytest.mark.parametrize("pin", _PINS, ids=_pin_id)
    def test_overwrite(self, pin: tuple[Any, list[str], str]) -> None:
        model, tables, access = pin
        expected = [statement.format(clause="OVERWRITE") for statement in [*tables, access]]
        assert model.access_ddl() == expected

    @pytest.mark.parametrize("pin", _PINS, ids=_pin_id)
    def test_if_not_exists(self, pin: tuple[Any, list[str], str]) -> None:
        model, tables, access = pin
        expected = [statement.format(clause="IF NOT EXISTS") for statement in [*tables, access]]
        assert model.access_ddl(overwrite=False) == expected

    @pytest.mark.parametrize("pin", _PINS, ids=_pin_id)
    def test_without_the_table(self, pin: tuple[Any, list[str], str]) -> None:
        model, _, access = pin
        assert model.access_ddl(with_table=False) == [access.format(clause="OVERWRITE")]
        assert model.access_ddl(overwrite=False, with_table=False) == [access.format(clause="IF NOT EXISTS")]

    @pytest.mark.parametrize("pin", _PINS, ids=_pin_id)
    def test_the_access_statement_is_the_parts_rendered(self, pin: tuple[Any, list[str], str]) -> None:
        model, _, access = pin
        parts = model._access_parts()
        assert access_statement(parts, clause="OVERWRITE") == access.format(clause="OVERWRITE")
        assert access_statement(parts, clause="IF NOT EXISTS") == access.format(clause="IF NOT EXISTS")


class TestAccessStatement:
    """Spec §5.4: ``schema.access_statement(parts, *, clause, comment=None)`` and ``AccessParts``."""

    _R20 = (
        "DEFINE ACCESS account ON DATABASE TYPE RECORD SIGNUP ( CREATE users SET email = $email, password = "
        "crypto::argon2::generate($password) ) SIGNIN ( SELECT * FROM users WHERE email = $email AND "
        "crypto::argon2::compare(password, $password) ) DURATION FOR TOKEN 15m, FOR SESSION 12h;"
    )
    _WHERE = "email = $email AND crypto::argon2::compare(password, $password)"

    def _parts(self, **changes: Any) -> AccessParts:
        values: dict[str, Any] = {
            "name": "account",
            "table": "users",
            "signup_fields": {"email": "$email", "password": "crypto::argon2::generate($password)"},
            "signin_where": self._WHERE,
        }
        values.update(changes)
        return AccessParts(**values)

    def test_r20(self) -> None:
        assert access_statement(self._parts(), clause="") == self._R20

    def test_r21_the_id_item_comes_first(self) -> None:
        fields = {"email": "$email", "id": "$email", "password": "crypto::argon2::generate($password)"}
        statement = access_statement(self._parts(signup_fields=fields), clause="")
        assert statement == self._R20.replace("SET email = $email", "SET id = $email, email = $email")

    def test_the_parts_keep_the_id_item_first(self) -> None:
        parts = self._parts(signup_fields={"b": "$b", "id": "$b", "a": "$a"})
        assert list(parts.signup_fields) == ["id", "b", "a"]

    def test_the_clauses(self) -> None:
        overwrite = access_statement(self._parts(), clause="OVERWRITE")
        missing = access_statement(self._parts(), clause="IF NOT EXISTS")
        assert overwrite == self._R20.replace("DEFINE ACCESS account", "DEFINE ACCESS OVERWRITE account")
        assert missing == self._R20.replace("DEFINE ACCESS account", "DEFINE ACCESS IF NOT EXISTS account")

    def test_a_comment_comes_after_the_durations(self) -> None:
        statement = access_statement(self._parts(), clause="", comment="x")
        assert statement == self._R20.removesuffix(";") + " COMMENT 'x';"
        quoted = access_statement(self._parts(), clause="", comment="it's")
        assert quoted.endswith("FOR SESSION 12h COMMENT 'it\\'s';")

    def test_with_refresh_writes_the_grant(self) -> None:
        statement = access_statement(self._parts(with_refresh=True, duration_grant="7d"), clause="")
        assert statement.endswith(
            "crypto::argon2::compare(password, $password) ) WITH REFRESH DURATION FOR TOKEN 15m, FOR SESSION 12h, FOR GRANT 7d;"
        )

    def test_with_refresh_defaults_the_grant_to_30d(self) -> None:
        statement = access_statement(self._parts(with_refresh=True), clause="")
        assert statement.endswith("FOR SESSION 12h, FOR GRANT 30d;")

    def test_the_durations(self) -> None:
        statement = access_statement(self._parts(duration_token="5m", duration_session="1h"), clause="")
        assert statement.endswith("DURATION FOR TOKEN 5m, FOR SESSION 1h;")

    def test_an_unknown_clause_is_refused(self) -> None:
        with pytest.raises(ValueError, match="clause= must be 'OVERWRITE', 'IF NOT EXISTS' or ''"):
            access_statement(self._parts(), clause="REPLACE")  # type: ignore[arg-type]

    def test_a_comment_with_a_nul_is_refused(self) -> None:
        with pytest.raises(ValueError, match="NUL"):
            access_statement(self._parts(), clause="", comment="a\x00b")

    def test_the_parts_are_frozen_and_read_only(self) -> None:
        parts = self._parts()
        with pytest.raises(dataclasses.FrozenInstanceError):
            parts.name = "other"  # type: ignore[misc]
        with pytest.raises(TypeError):
            parts.signup_fields["email"] = "$x"  # type: ignore[index]

    def test_the_model_parts(self) -> None:
        parts = AdKeyed._access_parts()
        assert parts == AccessParts(
            name="ad_keyed",
            table="AdKeyed",
            signup_fields={
                "id": "$email",
                "email": "$email",
                "password": "crypto::argon2::generate($password)",
                "name": "$name",
            },
            signin_where="email = $email AND crypto::argon2::compare(password, $password)",
            duration_token="15m",
            duration_session="12h",
            duration_grant=None,
            with_refresh=False,
        )
        refresh = AdRefresh._access_parts()
        assert (refresh.with_refresh, refresh.duration_grant) == (True, "7d")

    def test_the_model_parts_keep_the_checks(self) -> None:
        class AdBadAlgorithm(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(auth_algorithm="rot13")
            email: str
            password: str

        class AdBadDuration(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(auth_duration_token="soon")
            email: str
            password: str

        class AdIdKey(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(primary_key="id")
            id: str | None = None
            email: str
            password: str

        class AdExcluded(AuthenticatedUserMixin, BaseSurrealModel):
            email: str
            password: str = Field(exclude=True)

        with pytest.raises(ValueError, match="auth_algorithm='rot13'"):
            AdBadAlgorithm._access_parts()
        with pytest.raises(ValueError, match="auth_duration_token='soon'"):
            AdBadDuration._access_parts()
        with pytest.raises(ValueError, match="primary_key='id'"):
            AdIdKey._access_parts()
        with pytest.raises(TypeError, match="AdExcluded"):
            AdExcluded._access_parts()


# --- read_own(): the ORM's own statements ----------------------------------------------------------------------------


class _Color(StrEnum):
    RED = "red"
    BLUE = "blue"


class _MigAddress(BaseModel):
    city: str
    zip: int = 0


class _MigItem(BaseModel):
    label: str
    extra: Any = None


_ENGLISH = Analyzer("mig_english", ["class", "blank"], ["lowercase", "snowball(english)"], comment="English")


class MigPerson(BaseSurrealModel):
    """A SCHEMAFULL model with every declarable clause, rich types, a computed field, an untyped list, sub-models, an
    index trio (one with an analyzer) and two events."""

    model_config = SurrealConfigDict(
        table_name="mig_person",
        schema_mode=SchemaMode.SCHEMAFULL,
        comment="People",
        changefeed="7d INCLUDE ORIGINAL",
        permissions={"select": "WHERE $auth.id = id", "create": "FULL"},
        flexible_fields=["prefs"],
        indexes=[
            Index("mig_person_email", "email", unique=True, comment="it's unique"),
            Index("mig_person_bio", "bio", search_analyzer=_ENGLISH, bm25=True, highlights=True),
            Index("mig_person_emb", "emb", hnsw=True, dimension=3, dist="COSINE"),
        ],
        events=[
            Event("mig_audit", when="$event = 'CREATE'", then="CREATE mig_log SET who = $after.name", comment="audit"),
            Event("mig_every", then="CREATE mig_log SET a = 1"),
        ],
    )

    id: str | None = None
    name: str
    email: str
    age: int = 18
    score: float = 1.5
    price: Decimal = Decimal("1.10")
    joined: datetime = datetime(2026, 1, 1, tzinfo=UTC)
    color: _Color = _Color.RED
    kind: Literal["a", "b"] = "a"
    nick: str | None = None
    sep: str = "a;b"
    bio: str = ""
    tags: list[str] = []
    bag: list = []  # type: ignore[type-arg]
    anything: Any = None
    prefs: dict[str, Any] = {}
    emb: list[float] = []
    address: _MigAddress | None = None
    items: list[_MigItem] = []
    full: Computed[str] = computed("string::concat(name, ' ', email)")


class MigStats(BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_name="mig_stats", view_query="SELECT count() AS n FROM mig_person GROUP ALL", comment="Stats"
    )

    id: str | None = None
    n: int = 0


class MigLikes(BaseSurrealEdge):
    model_config = SurrealConfigDict(
        table_name="mig_likes",
        table_type=TableType.RELATION,
        relation_in="mig_person",
        relation_out=["mig_person", "mig_page"],
        enforced=True,
    )

    weight: int = 1


class MigUser(AuthenticatedUserMixin, BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_name="mig_user", access_name="mig_account", primary_key="email", schema_mode=SchemaMode.SCHEMAFULL
    )

    email: str
    password: str
    name: str = ""


_FUNCTION = function_ddl(
    "fn::mig_greet",
    "RETURN 'hi ' + $n;",
    parameters={"n": "string"},
    returns="string",
    comment="Greets",
    permissions="WHERE $auth.id != NONE",
)


def _own_statements() -> list[str]:
    """Every statement the appliers write for the models above, with its final ``;``."""
    return [
        *MigPerson.table_ddl(),
        *MigPerson.index_ddl(line=3),
        *MigPerson.index_ddl(line=2),
        *MigPerson.event_ddl(),
        MigStats.view_ddl(),
        *MigLikes.table_ddl(),
        *MigUser.access_ddl(),
        _FUNCTION,
    ]


_KINDS: dict[str, type] = {
    "TABLE": TableState,
    "FIELD": FieldState,
    "INDEX": IndexState,
    "ANALYZER": AnalyzerState,
    "EVENT": EventState,
    "ACCESS": AccessState,
    "FUNCTION": FunctionState,
}


def _read(statements: list[str], kind: type, name: str) -> Any:
    """The state ``read_own()`` gives for the statement of *kind* named *name* among *statements*."""
    for statement in statements:
        state = render.read_own(statement)
        if isinstance(state, kind) and state.name == name:
            return state
    raise AssertionError(f"no {kind.__name__} named {name!r}")


class TestReadOwn:
    """M17: ``read_own()`` reads every statement the appliers write."""

    @pytest.mark.parametrize("statement", _own_statements())
    def test_every_statement_is_read_to_its_state_class(self, statement: str) -> None:
        kind = re.match(r"DEFINE (\w+)", statement)
        assert kind is not None
        state = render.read_own(statement)
        assert isinstance(state, _KINDS[kind.group(1)])
        assert state.raw  # what the parser read

    def test_the_table(self) -> None:
        table = _read(MigPerson.table_ddl(), TableState, "mig_person")
        assert (table.schema_mode, table.table_type, table.comment) == ("SCHEMAFULL", "normal", "People")
        assert (table.changefeed, table.changefeed_include_original) == ("7d", True)
        assert table.permissions == {
            "select": "WHERE $auth.id = id",
            "create": "FULL",
            "update": "NONE",
            "delete": "NONE",
        }
        assert (table.view_query, table.drop, table.relation_in) == (None, False, ())

    @pytest.mark.parametrize(
        ("name", "field_type", "nullable", "default"),
        [
            ("name", "string", False, None),
            ("age", "int", False, "18"),
            ("score", "float", False, "1.5"),
            ("price", "decimal", False, "1.10dec"),
            ("joined", "datetime", False, "d'2026-01-01T00:00:00Z'"),
            ("color", "'red' | 'blue'", False, "'red'"),
            ("kind", "'a' | 'b'", False, "'a'"),
            ("nick", "string", True, None),
            ("sep", "string", False, "'a;b'"),
            ("tags", "array<string>", False, None),
            ("bag", "array", False, None),
            ("bag.*", "any", False, None),
            ("anything", "any", False, None),
            ("prefs", "object", False, None),
            ("address", "object", True, None),
            ("address.city", "string", False, None),
            ("address.zip", "int", False, "0"),
            ("items", "array<object>", False, None),
            ("items.*.label", "string", False, None),
            ("items.*.extra", "any", False, None),
        ],
    )
    def test_the_fields(self, name: str, field_type: str, nullable: bool, default: str | None) -> None:
        field = _read(MigPerson.table_ddl(), FieldState, name)
        assert (field.field_type, field.nullable, field.default) == (field_type, nullable, default)
        assert field.table == "mig_person"
        assert field.flexible is (name == "prefs")
        assert field.path == tuple(name.split("."))

    def test_the_computed_field(self) -> None:
        field = _read(MigPerson.table_ddl(), FieldState, "full")
        assert (field.field_type, field.nullable) == ("string", True)
        assert field.value == "string::concat(name, ' ', email)"
        assert field.computed is None

    def test_the_indexes_and_the_analyzer(self) -> None:
        statements = MigPerson.index_ddl(line=3)
        email = _read(statements, IndexState, "mig_person_email")
        assert (email.fields, email.unique, email.comment, email.table) == (("email",), True, "it's unique", "mig_person")
        bio = _read(statements, IndexState, "mig_person_bio")
        assert (bio.search_analyzer, bio.bm25, bio.highlights) == ("mig_english", True, True)
        # 2.x's SEARCH reads as 3.x's FULLTEXT.
        bio_2 = _read(MigPerson.index_ddl(line=2), IndexState, "mig_person_bio")
        assert "SEARCH" in bio_2.raw
        assert dataclasses.replace(bio_2, raw="") == dataclasses.replace(bio, raw="")
        emb = _read(statements, IndexState, "mig_person_emb")
        assert (emb.hnsw, emb.dimension, emb.dist, emb.vector_type, emb.efc, emb.m) == (True, 3, "COSINE", "F32", 150, 12)
        analyzer = _read(statements, AnalyzerState, "mig_english")
        assert (analyzer.tokenizers, analyzer.filters, analyzer.comment) == (
            ("class", "blank"),
            ("lowercase", "snowball(english)"),
            "English",
        )

    def test_the_events(self) -> None:
        audit = _read(MigPerson.event_ddl(), EventState, "mig_audit")
        assert (audit.table, audit.when, audit.then, audit.comment) == (
            "mig_person",
            "$event = 'CREATE'",
            "{ CREATE mig_log SET who = $after.name }",
            "audit",
        )
        every = _read(MigPerson.event_ddl(), EventState, "mig_every")
        # No WHEN: the servers render the default, WHEN true.
        assert (every.when, every.then, every.async_mode) == ("true", "{ CREATE mig_log SET a = 1 }", False)

    def test_an_async_event(self) -> None:
        statement = Event("e", then="CREATE log SET a = 1", async_mode=True, retry=2, max_depth=5).ddl("t")
        event = render.read_own(statement)
        assert isinstance(event, EventState)
        assert (event.async_mode, event.retry, event.max_depth, event.when) == (True, 2, 5, "true")

    def test_the_view(self) -> None:
        view = render.read_own(MigStats.view_ddl())
        assert isinstance(view, TableState)
        assert (view.name, view.view_query, view.comment) == (
            "mig_stats",
            "SELECT count() AS n FROM mig_person GROUP ALL",
            "Stats",
        )

    def test_a_view_with_every_clause(self) -> None:
        """The servers render a view's ``COMMENT`` before ``AS`` (measured on 3.3.4 and 2.7.0), where the parser reads it;
        ``view_statement()`` writes it after the query."""
        clauses = TableClauses(
            schema_mode="SCHEMAFULL",
            comment="it's (a) view AS x",
            changefeed="1d",
            permissions={"select": "FULL", "create": "NONE", "update": "NONE", "delete": "NONE"},
        )
        statement = view_statement("v", "SELECT * FROM src WHERE a = ')'", True, clauses)
        view = render.read_own(statement)
        assert isinstance(view, TableState)
        assert (view.schema_mode, view.comment, view.changefeed) == ("SCHEMAFULL", "it's (a) view AS x", "1d")
        assert view.view_query == "SELECT * FROM src WHERE a = ')'"
        assert view.permissions == {"select": "FULL", "create": "NONE", "update": "NONE", "delete": "NONE"}
        bare = render.read_own(view_statement("v", "SELECT * FROM src", False))
        assert isinstance(bare, TableState)
        assert (bare.view_query, bare.comment, bare.schema_mode) == ("SELECT * FROM src", None, "SCHEMALESS")
        # The servers' own form, without the parentheses, is read as it is.
        rendered = render.read_own("DEFINE TABLE `v` TYPE ANY SCHEMALESS COMMENT 'c' AS SELECT * FROM src;")
        assert isinstance(rendered, TableState)
        assert (rendered.view_query, rendered.comment) == ("SELECT * FROM src", "c")

    def test_the_edge(self) -> None:
        statements = MigLikes.table_ddl()
        likes = _read(statements, TableState, "mig_likes")
        assert (likes.table_type, likes.relation_in, likes.relation_out, likes.enforced) == (
            "relation",
            ("mig_person",),
            ("mig_person", "mig_page"),
            True,
        )
        assert _read(statements, FieldState, "weight").default == "1"

    def test_the_user_model(self) -> None:
        statements = MigUser.access_ddl()
        assert statements[-1].endswith(";")
        table = _read(statements, TableState, "mig_user")
        assert table.schema_mode == "SCHEMAFULL"
        access = _read(statements, AccessState, "mig_account")
        assert access.table == "mig_user"
        assert access.signup_fields == {
            "id": "$email",
            "email": "$email",
            "password": "crypto::argon2::generate($password)",
            "name": "$name",
        }
        assert access.signup_fields["id"] == "$email"
        assert access.signin_where == "email = $email AND crypto::argon2::compare(password, $password)"
        assert (access.duration_token, access.duration_session, access.duration_grant) == ("15m", "12h", None)
        assert (access.access_type, access.with_refresh, access.comment) == ("RECORD", False, None)

    def test_an_access_with_refresh_and_a_comment(self) -> None:
        parts = dataclasses.replace(MigUser._access_parts(), with_refresh=True, duration_grant="7d")
        access = render.read_own(access_statement(parts, clause="", comment="Users"))
        assert isinstance(access, AccessState)
        assert (access.with_refresh, access.duration_grant, access.comment) == (True, "7d", "Users")

    def test_the_function(self) -> None:
        function = render.read_own(_FUNCTION)
        assert isinstance(function, FunctionState)
        assert (function.name, function.parameters, function.returns) == ("fn::mig_greet", ("n",), "string")
        assert (function.comment, function.permissions) == ("Greets", "WHERE $auth.id != NONE")

    def test_the_three_forms_read_alike(self) -> None:
        overwrite = MigPerson.table_ddl()
        missing = MigPerson.table_ddl(overwrite=False)
        for one, other in zip(overwrite, missing, strict=True):
            first, second = render.read_own(one), render.read_own(other)
            assert dataclasses.replace(first, raw="") == dataclasses.replace(second, raw="")
            third = render.read_own(render.plain(one))
            assert dataclasses.replace(first, raw="") == dataclasses.replace(third, raw="")

    @pytest.mark.parametrize(
        "statement",
        [
            "DEFINE FIELD OVERWRITE `a` ON `t` TYPE int DEFAULT ('s3cr3t-marker';",
            "DEFINE TABLE OVERWRITE `t` SCHEMAFULL COMMENT 's3cr3t-marker;",
            "REMOVE TABLE `s3cr3t-marker`;",
            "DEFINE PARAM OVERWRITE $p VALUE 's3cr3t-marker';",
            "DEFINE EVENT OVERWRITE `e` ON `t` WHEN 's3cr3t-marker';",
            "DEFINE EVENT OVERWRITE `e` ON `t` COMMENT 's3cr3t-marker';",
            "DEFINE TABLE OVERWRITE `v` AS (SELECT * FROM src WHERE a = 's3cr3t-marker';",
            # A kind the parser keeps as written (an unknown clause after TYPE) is not one the ORM writes.
            "DEFINE FIELD OVERWRITE `a` ON `t` TYPE int FROBNICATE 's3cr3t-marker';",
        ],
    )
    def test_a_statement_it_cannot_read_is_a_bug(self, statement: str) -> None:
        with pytest.raises(RuntimeError) as raised:
            render.read_own(statement)
        assert "s3cr3t-marker" not in str(raised.value)
        assert "surreal_orm_lite" in str(raised.value)


# --- the render parts ------------------------------------------------------------------------------------------------


def _field(name: str, field_type: str, **changes: Any) -> FieldState:
    """A field as ``read_own()`` gives it back: not nullable unless said, on the table ``t``."""
    return FieldState(name=name, field_type=field_type, nullable=changes.pop("nullable", False), table="t", **changes)


def _state(*fields: FieldState, mode: str = "SCHEMAFULL", table: str = "t") -> SchemaState:
    return SchemaState(
        tables={table: TableState(table, schema_mode=mode, table_type="normal", fields={f.name: f for f in fields})}
    )


#: Spec R33's state *S*: ``t`` SCHEMAFULL with ``name``, ``bag``, ``tags`` and ``tags.*``.
_R33 = _state(_field("name", "string"), _field("bag", "any"), _field("tags", "array"), _field("tags.*", "any"))


class TestPlain:
    @pytest.mark.parametrize(
        ("statement", "expected"),
        [
            ("DEFINE TABLE OVERWRITE `t` SCHEMAFULL;", "DEFINE TABLE `t` SCHEMAFULL;"),
            ("DEFINE FIELD OVERWRITE `a` ON `t` TYPE int;", "DEFINE FIELD `a` ON `t` TYPE int;"),
            ("DEFINE INDEX OVERWRITE `i` ON `t` FIELDS `a`;", "DEFINE INDEX `i` ON `t` FIELDS `a`;"),
            ("DEFINE EVENT OVERWRITE `e` ON `t` THEN { CREATE x };", "DEFINE EVENT `e` ON `t` THEN { CREATE x };"),
            ("DEFINE ANALYZER OVERWRITE `a` TOKENIZERS blank;", "DEFINE ANALYZER `a` TOKENIZERS blank;"),
            (
                "DEFINE ACCESS OVERWRITE a ON DATABASE TYPE RECORD DURATION FOR TOKEN 15m;",
                "DEFINE ACCESS a ON DATABASE TYPE RECORD DURATION FOR TOKEN 15m;",
            ),
            ("DEFINE FUNCTION OVERWRITE fn::f() { RETURN 1; };", "DEFINE FUNCTION fn::f() { RETURN 1; };"),
        ],
    )
    def test_removes_the_overwrite_after_the_kind(self, statement: str, expected: str) -> None:
        assert render.plain(statement) == expected

    def test_an_overwrite_elsewhere_is_kept(self) -> None:
        assert render.plain("DEFINE TABLE OVERWRITE `t` COMMENT 'OVERWRITE';") == "DEFINE TABLE `t` COMMENT 'OVERWRITE';"
        assert (
            render.plain("DEFINE FUNCTION OVERWRITE fn::f() { RETURN ' OVERWRITE '; };")
            == "DEFINE FUNCTION fn::f() { RETURN ' OVERWRITE '; };"
        )
        assert render.plain("DEFINE TABLE OVERWRITE `OVERWRITE`;") == "DEFINE TABLE `OVERWRITE`;"

    @pytest.mark.parametrize(
        "statement",
        [
            "DEFINE TABLE `s3cr3t-marker` SCHEMAFULL;",
            "DEFINE TABLE IF NOT EXISTS `s3cr3t-marker`;",
            "REMOVE TABLE `s3cr3t-marker`;",
            "DEFINE TABLE `t` COMMENT 'OVERWRITE s3cr3t-marker';",
        ],
    )
    def test_without_the_overwrite_it_raises(self, statement: str) -> None:
        with pytest.raises(ValueError, match=r"DEFINE <KIND> OVERWRITE") as raised:
            render.plain(statement)
        assert "s3cr3t-marker" not in str(raised.value)


class TestRemoval:
    @pytest.mark.parametrize(
        ("arguments", "keywords", "expected"),
        [
            (("table", "log"), {}, "REMOVE TABLE `log`;"),
            (("table", "log"), {"if_exists": True}, "REMOVE TABLE IF EXISTS `log`;"),
            (("field", "age"), {"table": "person"}, "REMOVE FIELD `age` ON `person`;"),
            (("field", "age"), {"table": "person", "if_exists": True}, "REMOVE FIELD IF EXISTS `age` ON `person`;"),
            (("field", "address.city"), {"table": "person"}, "REMOVE FIELD `address`.`city` ON `person`;"),
            (("field", "tags.*"), {"table": "person"}, "REMOVE FIELD `tags`.* ON `person`;"),
            (("index", "person_email"), {"table": "person"}, "REMOVE INDEX `person_email` ON `person`;"),
            (
                ("index", "person_email"),
                {"table": "person", "if_exists": True},
                "REMOVE INDEX IF EXISTS `person_email` ON `person`;",
            ),
            (("event", "audit"), {"table": "person"}, "REMOVE EVENT `audit` ON `person`;"),
            (("event", "audit"), {"table": "person", "if_exists": True}, "REMOVE EVENT IF EXISTS `audit` ON `person`;"),
            (("analyzer", "english"), {}, "REMOVE ANALYZER `english`;"),
            (("analyzer", "english"), {"if_exists": True}, "REMOVE ANALYZER IF EXISTS `english`;"),
            (("access", "account"), {}, "REMOVE ACCESS `account` ON DATABASE;"),
            (("access", "account"), {"if_exists": True}, "REMOVE ACCESS IF EXISTS `account` ON DATABASE;"),
            (("function", "fn::greet"), {}, "REMOVE FUNCTION fn::greet;"),
            (("function", "greet"), {"if_exists": True}, "REMOVE FUNCTION IF EXISTS fn::greet;"),
        ],
    )
    def test_r23_and_r26(self, arguments: tuple[Any, str], keywords: dict[str, Any], expected: str) -> None:
        assert render.removal(*arguments, **keywords) == expected

    def test_r32_names_that_are_not_identifiers(self) -> None:
        assert render.removal("table", "user-events") == "REMOVE TABLE `user-events`;"
        assert render.removal("field", "first-name", table="user-events") == "REMOVE FIELD `first-name` ON `user-events`;"
        assert render.removal("index", "idx-x", table="user-events") == "REMOVE INDEX `idx-x` ON `user-events`;"
        assert render.removal("field", "a.b", table="t", path=("a.b",)) == "REMOVE FIELD `a.b` ON `t`;"
        assert render.removal("table", "2024_sales") == "REMOVE TABLE `2024_sales`;"
        assert render.removal("table", "we`ird") == "REMOVE TABLE `we\\`ird`;"

    @pytest.mark.parametrize("kind", ["field", "index", "event"])
    def test_a_table_kind_needs_its_table(self, kind: Any) -> None:
        with pytest.raises(ValueError, match="needs table="):
            render.removal(kind, "x")

    @pytest.mark.parametrize("name", ["", "a\x00b"])
    def test_an_empty_name_or_a_nul_is_refused(self, name: str) -> None:
        with pytest.raises(ValueError):
            render.removal("table", name)
        with pytest.raises(ValueError):
            render.removal("field", "a", table=name)

    def test_an_unknown_kind_is_refused(self) -> None:
        with pytest.raises(ValueError, match="kind"):
            render.removal("param", "x")  # type: ignore[arg-type]

    def test_a_name_that_is_not_a_str_is_refused(self) -> None:
        with pytest.raises(TypeError, match="removal\\(\\): the name must be a str, got int"):
            render.removal("table", 1)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="path segment"):
            render.removal("field", "a", table="t", path=("a", ""))


class TestFlexibleBefore3:
    """v0.34.0's rule, from a state: a SCHEMAFULL table, a kind holding ``any``, or a list of sub-models with a
    ``FLEXIBLE`` or ``any`` child."""

    _STATE = _state(
        _field("name", "string"),
        _field("bag", "any"),
        _field("tags", "array"),
        _field("tags.*", "any"),
        _field("opt", "any", nullable=True),
        _field("subs", "array<object>"),
        _field("subs.*.k", "any"),
        _field("items", "array<object>"),
        _field("items.*.label", "string"),
        _field("objs", "array<object>", nullable=True),
        _field("objs.*.o", "object", flexible=True),
        _field("deep", "array<object>"),
        _field("deep.*.inner", "array<object>"),
        _field("deep.*.inner.*.k", "any"),
        _field("plain", "object"),
        _field("plain.k", "any"),
    )

    @pytest.mark.parametrize(
        ("path", "kind", "expected"),
        [
            (("bag",), "any", True),
            (("tags",), "array", True),
            (("tags", "*"), "any", True),
            (("opt",), "any", True),
            (("subs",), "array<object>", True),
            (("deep",), "array<object>", True),
            (("deep", "*", "inner"), "array<object>", True),
            (("objs",), "option<array<object>>", True),
            (("name",), "string", False),
            (("items",), "array<object>", False),
            (("plain",), "object", False),
            (("missing",), "array<object>", False),
        ],
    )
    def test_on_a_schemafull_table(self, path: tuple[str, ...], kind: str, expected: bool) -> None:
        assert render.flexible_before_3(self._STATE, "t", path, kind) is expected

    def test_never_on_a_schemaless_table(self) -> None:
        state = _state(_field("bag", "any"), _field("subs", "array<object>"), _field("subs.*.k", "any"), mode="SCHEMALESS")
        assert render.flexible_before_3(state, "t", ("bag",), "any") is False
        assert render.flexible_before_3(state, "t", ("subs",), "array<object>") is False

    def test_never_without_the_table_or_the_state(self) -> None:
        assert render.flexible_before_3(self._STATE, "other", ("bag",), "any") is False
        assert render.flexible_before_3(None, "t", ("bag",), "any") is False

    def test_a_child_whose_kind_is_unreadable_does_not_select_its_list(self) -> None:
        state = _state(_field("subs", "array<object>"), _field("subs.*.k", "frob<"))
        assert render.flexible_before_3(state, "t", ("subs",), "array<object>") is False

    def test_an_unreadable_kind_is_refused(self) -> None:
        with pytest.raises(ValueError, match="flexible_before_3\\(\\): the kind is not a SurrealQL kind the ORM can read"):
            render.flexible_before_3(self._STATE, "t", ("bag",), "array<")


class TestFlexibleCandidate:
    """P2: whether ``flexible_before_3()`` could select a kind on some state."""

    @pytest.mark.parametrize(
        "kind",
        [
            "any",
            "option<any>",
            "array",
            "set",
            "array<any>",
            "set<any>",
            "array<object>",
            "option<array<object>>",
            "set<object>",
        ],
    )
    def test_true(self, kind: str) -> None:
        assert render.flexible_candidate(kind) is True

    @pytest.mark.parametrize(
        "kind", ["string", "object", "option<int>", "array<string>", "record<t>", "option<object>", "array<int, 3>"]
    )
    def test_false(self, kind: str) -> None:
        assert render.flexible_candidate(kind) is False

    def test_an_unreadable_kind_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not a SurrealQL kind the ORM can read"):
            render.flexible_candidate("array<")


class TestFieldStatement:
    """Spec §6.1: the one field writer, every clause a ``FieldState`` holds in the servers' order, read back."""

    _FULL = _field(
        "d",
        "int",
        nullable=True,
        reference=True,
        on_delete="CASCADE",
        default="7",
        default_always=True,
        readonly=True,
        value="$value OR 7",
        assertion="$value > 0",
        permissions={"select": "FULL", "create": "NONE", "update": "WHERE $auth.id = id"},
        comment="it's d",
    )
    _COMPUTED = _field(
        "c", "int", computed="a + 1", permissions={"select": "NONE", "create": "NONE", "update": "NONE"}, comment="c"
    )

    def test_every_clause_in_order(self) -> None:
        assert render.field_statement("t", self._FULL, mode="plain", line=3, state=None) == (
            "DEFINE FIELD `d` ON `t` TYPE option<int> REFERENCE ON DELETE CASCADE DEFAULT ALWAYS 7 READONLY "
            "VALUE $value OR 7 ASSERT $value > 0 COMMENT 'it\\'s d' PERMISSIONS FOR select FULL, FOR create NONE, "
            "FOR update WHERE $auth.id = id;"
        )

    def test_a_computed_field(self) -> None:
        assert render.field_statement("t", self._COMPUTED, mode="plain", line=3, state=None) == (
            "DEFINE FIELD `c` ON `t` TYPE int COMPUTED a + 1 COMMENT 'c' PERMISSIONS NONE;"
        )

    @pytest.mark.parametrize("field", [_FULL, _COMPUTED], ids=["full", "computed"])
    def test_read_own_gives_the_field_back(self, field: FieldState) -> None:
        statement = render.field_statement("t", field, mode="overwrite", line=3, state=None)
        assert dataclasses.replace(render.read_own(statement), raw="") == field

    @pytest.mark.parametrize(
        "field",
        [
            _field("plain", "string"),
            _field("opt", "string", nullable=True),
            _field("lit", "'a' | 'b'", nullable=True, default="'a'"),
            _field("rec", "record<person>", nullable=True, reference=True),
            _field("rec2", "record<person>", reference=True, on_delete="THEN UPDATE $this SET x = 1"),
            _field("d", "datetime", default="time::now()", readonly=True),
            _field("obj", "object", flexible=True),
            _field("tags.*", "any"),
            _field("a.b", "string", path=("a.b",)),
        ],
        ids=lambda field: field.name,
    )
    def test_more_fields_round_trip(self, field: FieldState) -> None:
        statement = render.field_statement("t", field, mode="plain", line=3, state=None)
        assert dataclasses.replace(render.read_own(statement), raw="") == field

    def test_the_modes(self) -> None:
        field = _field("a", "int")
        assert render.field_statement("t", field, mode="plain", line=3, state=None) == "DEFINE FIELD `a` ON `t` TYPE int;"
        assert (
            render.field_statement("t", field, mode="overwrite", line=3, state=None)
            == "DEFINE FIELD OVERWRITE `a` ON `t` TYPE int;"
        )
        assert (
            render.field_statement("t", field, mode="if_not_exists", line=3, state=None)
            == "DEFINE FIELD IF NOT EXISTS `a` ON `t` TYPE int;"
        )
        with pytest.raises(ValueError, match="mode="):
            render.field_statement("t", field, mode="replace", line=3, state=None)  # type: ignore[arg-type]

    def test_the_line_is_checked(self) -> None:
        with pytest.raises(ValueError, match="line= must be 2 or 3"):
            render.field_statement("t", _field("a", "int"), mode="plain", line=4, state=None)  # type: ignore[arg-type]

    def test_r25_flexible_before_3(self) -> None:
        state = _state(_field("bag", "any"))
        bag = _field("bag", "any")
        assert (
            render.field_statement("t", bag, mode="plain", line=2, state=state)
            == "DEFINE FIELD `bag` ON `t` TYPE any FLEXIBLE;"
        )
        assert render.field_statement("t", bag, mode="plain", line=3, state=state) == "DEFINE FIELD `bag` ON `t` TYPE any;"
        assert render.field_statement("t", bag, mode="plain", line=2, state=None) == "DEFINE FIELD `bag` ON `t` TYPE any;"
        schemaless = _state(_field("bag", "any"), mode="SCHEMALESS")
        assert render.field_statement("t", bag, mode="plain", line=2, state=schemaless) == "DEFINE FIELD `bag` ON `t` TYPE any;"

    def test_an_optional_any_is_written_any(self) -> None:
        """``option<any>`` is a parse error on both lines (measured on 3.3.4 and 2.7.0): ``any`` admits ``NONE``."""
        field = _field("bag", "any", nullable=True)
        assert render.field_statement("t", field, mode="plain", line=3, state=None) == "DEFINE FIELD `bag` ON `t` TYPE any;"

    def test_a_flexible_field(self) -> None:
        obj = _field("obj", "object", flexible=True)
        assert (
            render.field_statement("t", obj, mode="plain", line=3, state=None)
            == "DEFINE FIELD `obj` ON `t` TYPE object FLEXIBLE;"
        )
        # SurrealDB 3.x refuses FLEXIBLE on a kind holding no object; a 2.x reading holds it on `any` columns.
        bag = _field("bag", "any", flexible=True)
        assert render.field_statement("t", bag, mode="plain", line=3, state=None) == "DEFINE FIELD `bag` ON `t` TYPE any;"
        assert (
            render.field_statement("t", bag, mode="plain", line=2, state=None) == "DEFINE FIELD `bag` ON `t` TYPE any FLEXIBLE;"
        )

    def test_an_unreadable_kind_is_refused(self) -> None:
        with pytest.raises(ValueError, match="the kind of field 'a' is not a SurrealQL kind the ORM can read"):
            render.field_statement("t", _field("a", "array<"), mode="plain", line=3, state=None)

    def test_a_comment_with_a_nul_is_refused(self) -> None:
        with pytest.raises(ValueError, match="NUL"):
            render.field_statement("t", _field("a", "int", comment="a\x00b"), mode="plain", line=3, state=None)


class TestFlexibleRedefinitions:
    """Spec R33: the columns an ``AlterTable`` making ``t`` SCHEMAFULL redefines on line 2."""

    _LINES = [
        "DEFINE FIELD OVERWRITE `bag` ON `t` TYPE any FLEXIBLE;",
        "DEFINE FIELD OVERWRITE `tags` ON `t` TYPE array FLEXIBLE;",
        "DEFINE FIELD OVERWRITE `tags`.* ON `t` TYPE any FLEXIBLE;",
    ]

    def test_r33(self) -> None:
        assert render.flexible_redefinitions("t", _R33, skip=()) == self._LINES

    def test_skip(self) -> None:
        assert render.flexible_redefinitions("t", _R33, skip=("tags.*",)) == self._LINES[:2]

    def test_none_on_a_schemaless_table_or_a_missing_one(self) -> None:
        schemaless = _state(_field("bag", "any"), _field("tags", "array"), mode="SCHEMALESS")
        assert render.flexible_redefinitions("t", schemaless, skip=()) == []
        assert render.flexible_redefinitions("other", _R33, skip=()) == []

    def test_every_clause_is_kept(self) -> None:
        state = _state(_field("bag", "any", default="{}", comment="kept", assertion="$value != NONE"))
        assert render.flexible_redefinitions("t", state, skip=()) == [
            "DEFINE FIELD OVERWRITE `bag` ON `t` TYPE any FLEXIBLE DEFAULT {} ASSERT $value != NONE COMMENT 'kept';"
        ]

    def test_skip_is_a_collection(self) -> None:
        with pytest.raises(TypeError, match="skip= takes a collection of field names, not a str"):
            render.flexible_redefinitions("t", _R33, skip="tags.*")

    def test_a_column_whose_kind_is_unreadable_is_left_out(self) -> None:
        state = _state(_field("bag", "any"), _field("odd", "frob<"))
        assert render.flexible_redefinitions("t", state, skip=()) == [self._LINES[0]]

    def test_a_list_of_sub_models(self) -> None:
        state = _state(_field("subs", "array<object>"), _field("subs.*.k", "any"), _field("subs.*.n", "string"))
        assert render.flexible_redefinitions("t", state, skip=()) == [
            "DEFINE FIELD OVERWRITE `subs` ON `t` TYPE array<object> FLEXIBLE;",
            "DEFINE FIELD OVERWRITE `subs`.*.`k` ON `t` TYPE any FLEXIBLE;",
        ]


# --- literal_value() and check_statements() --------------------------------------------------------------------------


class TestLiteralValue:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("true", True),
            ("false", False),
            ("-3", -3),
            ("0", 0),
            ("1.5", 1.5),
            ("1.5f", 1.5),
            ("-1.5f", -1.5),
            ("2f", 2.0),
            ("1.10dec", Decimal("1.10")),
            ("-2dec", Decimal("-2")),
            ("'a;b'", "a;b"),
            ('"it\'s"', "it's"),
            ("'it\\'s'", "it's"),
            ("''", ""),
            ("d'2026-01-01T00:00:00Z'", datetime(2026, 1, 1, tzinfo=UTC)),
            ("d'2026-01-01T00:00:00.5Z'", datetime(2026, 1, 1, 0, 0, 0, 500000, tzinfo=UTC)),
            ("d'2026-01-01T00:00:00.123456Z'", datetime(2026, 1, 1, 0, 0, 0, 123456, tzinfo=UTC)),
        ],
    )
    def test_a_literal_the_orm_can_write(self, text: str, expected: object) -> None:
        value = render.literal_value(text)
        assert value == expected
        assert type(value) is type(expected)

    def test_a_datetime_is_aware_utc(self) -> None:
        value = render.literal_value("d'2026-01-01T00:00:00Z'")
        assert isinstance(value, datetime)
        assert value.utcoffset() is not None and value.utcoffset().total_seconds() == 0  # type: ignore[union-attr]

    @pytest.mark.parametrize(
        "text",
        [
            "NONE",
            "NULL",
            "[]",
            "{}",
            "time::now()",
            "{ LET $x = 1; RETURN $x; }",
            "d'2026-01-01T00:00:00.1234567Z'",
            "d'2026-13-01T00:00:00Z'",
            "d'2026-01-01T00:00:00+01:00'",
            "u'0189a4d6-58a7-7c1e-9e36-6d4c4b1c0f00'",
            "1e3",
            "NaN",
            "9223372036854775808",
            "'a\\0b'",
            "TRUE",
            "'unterminated",
            "1.5 + 1",
            "",
        ],
    )
    def test_anything_else_is_none(self, text: str) -> None:
        assert render.literal_value(text) is None


class TestCheckStatements:
    def test_counts_the_top_level_statements(self) -> None:
        assert render.check_statements("UPDATE a SET b = 1; UPDATE c SET d = 2;", what="RawSQL") == 2
        assert render.check_statements("UPDATE a SET b = 1; UPDATE c SET d = 2", what="RawSQL") == 2
        assert render.check_statements("DEFINE TABLE c;;", what="RawSQL") == 1  # M2: an empty statement has no entry
        assert render.check_statements("UPDATE a SET b = 'x;y';", what="RawSQL") == 1
        assert render.check_statements("DEFINE EVENT e ON t THEN { CREATE a; CREATE b; };", what="RawSQL") == 1
        assert render.check_statements("SELECT * FROM user; user:1.name;", what="RawSQL") == 2
        assert render.check_statements("", what="RawSQL") == 0
        assert render.check_statements("  ;  ; ", what="RawSQL") == 0
        assert render.check_statements("DEFINE TABLE a;\nDEFINE FIELD b ON a TYPE int;", what="RawSQL") == 2

    @pytest.mark.parametrize(
        "sql",
        [
            "BEGIN TRANSACTION; UPDATE a SET b = 1; COMMIT TRANSACTION;",
            "BEGIN;",
            "UPDATE a SET b = 1; COMMIT;",
            "CANCEL TRANSACTION;",
            "USE NS x;",
            "UPDATE a SET b = 1; use db y",
            "begin transaction",
        ],
    )
    def test_a_transaction_or_use_is_refused(self, sql: str) -> None:
        with pytest.raises(ValueError) as raised:
            render.check_statements(sql, what="RawSQL")
        assert str(raised.value) == (
            "RawSQL: BEGIN, COMMIT, CANCEL and USE are not allowed; each migration runs in a transaction of its own."
        )

    def test_the_owner_is_named(self) -> None:
        with pytest.raises(ValueError, match=r"^DataMigration: BEGIN, COMMIT, CANCEL and USE are not allowed"):
            render.check_statements("COMMIT;", what="DataMigration")

    @pytest.mark.parametrize(
        ("sql", "reason"),
        [
            ("UPDATE a SET b = 1 -- s3cr3t-marker", "comment marker"),
            ("UPDATE a SET b = 1; # s3cr3t-marker", "comment marker"),
            ("UPDATE a SET b = 's3cr3t-marker", "string literal is not terminated"),
            ("UPDATE a SET b = 1\x00 s3cr3t-marker", "NUL"),
            ("UPDATE a SET b = (1 s3cr3t-marker", "brackets"),
        ],
    )
    def test_text_the_scanner_refuses(self, sql: str, reason: str) -> None:
        with pytest.raises(ValueError, match=reason) as raised:
            render.check_statements(sql, what="RawSQL")
        assert str(raised.value).startswith("RawSQL: the SQL is not SurrealQL the ORM can send: ")
        assert "s3cr3t-marker" not in str(raised.value)

    def test_a_non_str_is_refused(self) -> None:
        with pytest.raises(TypeError, match="RawSQL: the SQL must be a str, got int"):
            render.check_statements(1, what="RawSQL")  # type: ignore[arg-type]
