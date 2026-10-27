"""Migration system (v0.38.0): the operations and their statements — the access statement, ``migrations/render.py``,
``migrations/operations.py`` and ``migrations/state.py``'s ``place()``/``take_out()``.

The operations (from ``TestGoldens`` on): spec §5.4's goldens R1–R33 compared exactly (``TestGoldens``,
``TestMigrationSql`` for ``Migration.forwards_sql()``/``backwards_sql()`` and ``skip_fields``), the golden equalities
with the appliers (``TestGoldenEqualities``), every construction refusal of §5.3 (``TestOperationChecks``,
``TestEdges``), the names of D26 (``TestNames``), ``reversible``/``destructive`` (``TestReversible``),
``describe()``, ``state_forwards()``/``state_backwards()`` (``TestStateForwards``, ``TestStateBackwards``: SurrealDB's
DDL semantics, D31 as amended, C8, C9, D39), ``split_destructive()``, the ``from_*`` class methods
(``TestFromState``), ``DefaultExpression`` (D23), ``repr()`` without SurrealQL text and ``pickle``.

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
import pickle
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
    SurrealFunc,
    TableType,
    computed,
)
from surreal_orm_lite.define_parser import parse_define_access
from surreal_orm_lite.migrations import (
    AddField,
    AlterField,
    AlterTable,
    CreateIndex,
    CreateTable,
    DataMigration,
    DefaultExpression,
    DefineAccess,
    DefineAnalyzer,
    DefineEvent,
    DefineFunction,
    DropField,
    DropIndex,
    DropTable,
    Migration,
    Operation,
    RawSQL,
    RebuildIndex,
    RemoveAccess,
    RemoveAnalyzer,
    RemoveEvent,
    RemoveFunction,
    render,
    split_destructive,
)
from surreal_orm_lite.migrations import state as mstate
from surreal_orm_lite.migrations.migration import _statements
from surreal_orm_lite.migrations.operations import _block_content
from surreal_orm_lite.schema import AccessParts, TableClauses, access_statement, function_ddl, view_statement
from surreal_orm_lite.schema_state import (
    AccessState,
    AnalyzerState,
    EventState,
    FieldState,
    FunctionState,
    IndexState,
    SchemaIssue,
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


# === Task 3: the operations ==========================================================================================
#
# Spec §5.3 and §5.4: the goldens R1–R33 (each statement compared exactly), the golden equalities with the appliers,
# every construction refusal, the names (D26), ``reversible``/``destructive``/``describe()``, ``state_forwards()`` and
# ``state_backwards()`` (§6.2, D31 as amended, C8, C9, D39), ``split_destructive()``, the ``from_*`` class methods,
# ``DefaultExpression`` (D23), ``repr()`` without SurrealQL text and ``pickle``.

R1 = CreateTable(
    name="person",
    schema_mode="SCHEMAFULL",
    table_type="normal",
    comment="People",
    changefeed="7d",
    permissions={"select": "WHERE $auth.id = id", "create": "FULL"},
)
R2 = CreateTable(name="log")
R3 = CreateTable(name="person_stats", schema_mode="SCHEMALESS", view_query="SELECT count() AS n FROM person GROUP ALL")
R4 = CreateTable(
    name="likes", schema_mode="SCHEMALESS", table_type="relation", relation_in="person", relation_out=["person", "page"]
)
R5 = AlterTable(name="person", schema_mode="SCHEMALESS", previous_schema_mode="SCHEMAFULL", previous_comment="People")
R6 = AddField(table="person", name="age", field_type="int", default=18)
R7 = AddField(table="person", name="nick", field_type="string", nullable=True, default="n/a")
R8 = AddField(table="person", name="full", field_type="string", nullable=True, value="string::concat(name, ' ', email)")
R9 = AddField(
    table="person",
    name="joined",
    field_type="datetime",
    default=SurrealFunc("time::now()"),
    readonly=True,
    assertion="$value <= time::now()",
    comment="Join date",
    permissions={"update": "NONE"},
)
R10 = AddField(table="person", name="address.city", field_type="string")
R11 = AlterField(table="person", name="age", field_type="int", default=21, previous_type="int", previous_default=18)
R12 = DropField(table="person", name="age")
R13 = CreateIndex(table="person", name="person_email", fields=["email"], unique=True)
R14 = CreateIndex(table="post", name="post_body", fields=["body"], search_analyzer="english", bm25=True, highlights=True)
R15 = CreateIndex(table="doc", name="doc_emb", fields=["emb"], hnsw=True, dimension=3, dist="COSINE")
R16 = CreateIndex(
    table="person",
    name="person_email",
    fields=["email"],
    unique=True,
    previous=CreateIndex(table="person", name="person_email", fields=["email"]),
)
R17 = DefineAnalyzer(name="english", tokenizers=["class", "blank"], filters=["lowercase", "snowball(english)"])
R18 = DefineEvent("audit", "person", "$event = 'CREATE'", "CREATE log SET who = $after.name")
R19 = DefineEvent("audit", "person", None, "CREATE log SET a = 1", async_mode=True)
_SIGNIN = "email = $email AND crypto::argon2::compare(password, $password)"
R20 = DefineAccess(
    name="account",
    table="users",
    signup_fields={"email": "$email", "password": "crypto::argon2::generate($password)"},
    signin_where=_SIGNIN,
)
R21 = DefineAccess(
    name="account",
    table="users",
    signup_fields={"email": "$email", "id": "$email", "password": "crypto::argon2::generate($password)"},
    signin_where=_SIGNIN,
)
R22 = DefineFunction("fn::greet", "RETURN 'hi ' + $n;", parameters={"n": "string"}, returns="string")
R24 = RebuildIndex("post", "post_body", if_exists=True)
R28 = AddField(table="t", name="d", field_type="int", default=7, default_always=True)
R29 = AlterField(table="t", name="c", field_type="int", computed="a + 1", previous_type="int", previous_computed="a + 2")
R30 = CreateTable(name="cf", schema_mode="SCHEMALESS", table_type="normal", changefeed="1h INCLUDE ORIGINAL", drop=True)
R33 = AlterTable(
    name="t", schema_mode="SCHEMAFULL", table_type="normal", previous_schema_mode="SCHEMALESS", previous_table_type="normal"
)
#: R33's reverse: back to SCHEMALESS, its backwards restoring SCHEMAFULL.
R33_REVERSE = AlterTable(
    name="t", schema_mode="SCHEMALESS", table_type="normal", previous_schema_mode="SCHEMAFULL", previous_table_type="normal"
)

_R1_TEXT = (
    "DEFINE TABLE `person` TYPE NORMAL SCHEMAFULL COMMENT 'People' CHANGEFEED 7d PERMISSIONS FOR select WHERE "
    "$auth.id = id, FOR create FULL, FOR update, delete NONE;"
)
_R13_TEXT = "DEFINE INDEX `person_email` ON `person` FIELDS `email` UNIQUE;"
_R18_TEXT = "DEFINE EVENT `audit` ON `person` WHEN $event = 'CREATE' THEN { CREATE log SET who = $after.name };"
_R20_TEXT = (
    "DEFINE ACCESS account ON DATABASE TYPE RECORD SIGNUP ( CREATE users SET email = $email, password = "
    "crypto::argon2::generate($password) ) SIGNIN ( SELECT * FROM users WHERE email = $email AND "
    "crypto::argon2::compare(password, $password) ) DURATION FOR TOKEN 15m, FOR SESSION 12h;"
)
_R22_TEXT = "DEFINE FUNCTION fn::greet($n: string) -> string { RETURN 'hi ' + $n; };"
_R33_LINES = [
    "DEFINE TABLE OVERWRITE `t` TYPE NORMAL SCHEMAFULL;",
    "DEFINE FIELD OVERWRITE `bag` ON `t` TYPE any FLEXIBLE;",
    "DEFINE FIELD OVERWRITE `tags` ON `t` TYPE array FLEXIBLE;",
    "DEFINE FIELD OVERWRITE `tags`.* ON `t` TYPE any FLEXIBLE;",
]


def _replay(*operations: Operation, start: SchemaState | None = None) -> SchemaState:
    """The state each operation's ``state_forwards()`` gives in turn, from *start* (an empty state)."""
    state = SchemaState() if start is None else start
    for operation in operations:
        state = operation.state_forwards(state)
    return state


def _r33_state() -> SchemaState:
    """Spec R33's state *S*: ``t`` created SCHEMALESS with ``name``, ``bag``, ``tags`` and ``tags.*``, then R33."""
    return _replay(
        CreateTable(name="t", schema_mode="SCHEMALESS", table_type="normal"),
        AddField("t", "name", "string"),
        AddField("t", "bag", "any"),
        AddField("t", "tags", "array"),
        AddField("t", "tags.*", "any"),
        R33,
    )


class TestGoldens:
    """Spec §5.4: each statement compared with the exact literal."""

    def test_r1(self) -> None:
        assert R1.forwards() == _R1_TEXT

    def test_r2(self) -> None:
        assert R2.forwards() == "DEFINE TABLE `log` SCHEMAFULL;"

    def test_r3(self) -> None:
        assert R3.forwards() == "DEFINE TABLE `person_stats` SCHEMALESS AS (SELECT count() AS n FROM person GROUP ALL);"

    def test_r4(self) -> None:
        assert R4.forwards() == "DEFINE TABLE `likes` TYPE RELATION IN `person` OUT `person` | `page` SCHEMALESS;"

    def test_r5(self) -> None:
        assert R5.forwards() == "DEFINE TABLE OVERWRITE `person` SCHEMALESS;"
        assert R5.backwards() == "DEFINE TABLE OVERWRITE `person` SCHEMAFULL COMMENT 'People';"

    def test_r6_to_r10(self) -> None:
        assert R6.forwards() == "DEFINE FIELD `age` ON `person` TYPE int DEFAULT 18;"
        assert R7.forwards() == "DEFINE FIELD `nick` ON `person` TYPE option<string> DEFAULT 'n/a';"
        assert R8.forwards() == ("DEFINE FIELD `full` ON `person` TYPE option<string> VALUE string::concat(name, ' ', email);")
        assert R9.forwards() == (
            "DEFINE FIELD `joined` ON `person` TYPE datetime DEFAULT time::now() READONLY ASSERT $value <= time::now() "
            "COMMENT 'Join date' PERMISSIONS FOR select, create FULL, FOR update NONE;"
        )
        assert R10.forwards() == "DEFINE FIELD `address`.`city` ON `person` TYPE string;"

    def test_r11(self) -> None:
        assert R11.forwards() == "DEFINE FIELD OVERWRITE `age` ON `person` TYPE int DEFAULT 21;"
        assert R11.backwards() == "DEFINE FIELD OVERWRITE `age` ON `person` TYPE int DEFAULT 18;"

    def test_r12(self) -> None:
        assert R12.forwards() == "REMOVE FIELD `age` ON `person`;"
        assert R12.backwards() == ""

    def test_r13(self) -> None:
        assert R13.forwards() == _R13_TEXT

    def test_r14_per_line(self) -> None:
        assert R14.forwards() == (
            "DEFINE INDEX `post_body` ON `post` FIELDS `body` FULLTEXT ANALYZER `english` BM25 HIGHLIGHTS;"
        )
        assert R14.forwards(line=3) == R14.forwards()
        assert (
            R14.forwards(line=2)
            == "DEFINE INDEX `post_body` ON `post` FIELDS `body` SEARCH ANALYZER `english` BM25 HIGHLIGHTS;"
        )

    def test_r15(self) -> None:
        assert R15.forwards() == (
            "DEFINE INDEX `doc_emb` ON `doc` FIELDS `emb` HNSW DIMENSION 3 DIST COSINE TYPE F32 EFC 150 M 12;"
        )

    def test_r16(self) -> None:
        assert R16.forwards() == "DEFINE INDEX OVERWRITE `person_email` ON `person` FIELDS `email` UNIQUE;"
        assert R16.backwards() == "DEFINE INDEX OVERWRITE `person_email` ON `person` FIELDS `email`;"

    def test_r17(self) -> None:
        assert (
            R17.forwards() == "DEFINE ANALYZER OVERWRITE `english` TOKENIZERS class,blank FILTERS lowercase,snowball(english);"
        )
        assert R17.backwards() == "REMOVE ANALYZER IF EXISTS `english`;"

    def test_r18_r19(self) -> None:
        assert R18.forwards() == _R18_TEXT
        assert R19.forwards() == "DEFINE EVENT `audit` ON `person` ASYNC THEN { CREATE log SET a = 1 };"

    def test_r20(self) -> None:
        assert R20.forwards() == _R20_TEXT

    @pytest.mark.parametrize("line", [2, 3])
    def test_r21_the_id_item_first_on_every_line(self, line: Literal[2, 3]) -> None:
        assert R21.forwards(line=line) == _R20_TEXT.replace("SET email = $email", "SET id = $email, email = $email")

    def test_r22(self) -> None:
        assert R22.forwards() == _R22_TEXT

    def test_r23(self) -> None:
        removals: list[Operation] = [
            RemoveFunction("fn::greet"),
            RemoveEvent("audit", "person"),
            RemoveAnalyzer("english"),
            RemoveAccess("account"),
            DropIndex("person", "person_email"),
            DropTable("log"),
        ]
        assert [operation.forwards() for operation in removals] == [
            "REMOVE FUNCTION fn::greet;",
            "REMOVE EVENT `audit` ON `person`;",
            "REMOVE ANALYZER `english`;",
            "REMOVE ACCESS `account` ON DATABASE;",
            "REMOVE INDEX `person_email` ON `person`;",
            "REMOVE TABLE `log`;",
        ]
        assert [operation.backwards() for operation in removals] == [""] * 6

    def test_r24(self) -> None:
        assert R24.forwards() == "REBUILD INDEX IF EXISTS `post_body` ON `post`;"
        assert R24.backwards() == R24.forwards()
        assert RebuildIndex("post", "post_body").forwards() == "REBUILD INDEX `post_body` ON `post`;"

    def test_r25(self) -> None:
        state = CreateTable(name="t").state_forwards(SchemaState())
        bag = AddField(table="t", name="bag", field_type="any")
        assert bag.forwards(line=2, state=state) == "DEFINE FIELD `bag` ON `t` TYPE any FLEXIBLE;"
        assert bag.forwards(line=3, state=state) == "DEFINE FIELD `bag` ON `t` TYPE any;"
        assert bag.forwards(line=2) == "DEFINE FIELD `bag` ON `t` TYPE any;"  # without a state: nothing more

    def test_r26_creations_are_undone_if_they_exist(self) -> None:
        assert [operation.backwards() for operation in (R2, R6, R13, R18, R20, R22)] == [
            "REMOVE TABLE IF EXISTS `log`;",
            "REMOVE FIELD IF EXISTS `age` ON `person`;",
            "REMOVE INDEX IF EXISTS `person_email` ON `person`;",
            "REMOVE EVENT IF EXISTS `audit` ON `person`;",
            "REMOVE ACCESS IF EXISTS `account` ON DATABASE;",
            "REMOVE FUNCTION IF EXISTS fn::greet;",
        ]

    def test_r27_removals_with_previous(self) -> None:
        assert RemoveEvent("audit", "person", previous=R18).backwards() == _R18_TEXT
        assert RemoveFunction("fn::greet", previous=R22).backwards() == _R22_TEXT
        assert DropIndex("person", "person_email", previous=R13).backwards() == _R13_TEXT
        assert RemoveAccess("account", previous=R20).backwards() == _R20_TEXT
        assert RemoveAnalyzer("english", previous=R17).backwards() == R17.forwards()  # always OVERWRITE

    def test_r28(self) -> None:
        assert R28.forwards() == "DEFINE FIELD `d` ON `t` TYPE int DEFAULT ALWAYS 7;"

    def test_r29(self) -> None:
        assert R29.forwards() == "DEFINE FIELD OVERWRITE `c` ON `t` TYPE int COMPUTED a + 1;"
        assert R29.backwards() == "DEFINE FIELD OVERWRITE `c` ON `t` TYPE int COMPUTED a + 2;"

    def test_r30(self) -> None:
        assert R30.forwards() == "DEFINE TABLE `cf` TYPE NORMAL DROP SCHEMALESS CHANGEFEED 1h INCLUDE ORIGINAL;"

    def test_r31(self) -> None:
        sep = AddField.from_field_state("t", FieldState("sep", "string", False, "'a;b'"))
        assert sep == AddField(table="t", name="sep", field_type="string", default="a;b")
        assert sep.forwards() == "DEFINE FIELD `sep` ON `t` TYPE string DEFAULT 'a;b';"
        block = AddField.from_field_state("t", FieldState("sep", "string", False, "{ LET $x = 1; RETURN $x; }"))
        assert block.default == DefaultExpression("{ LET $x = 1; RETURN $x; }")
        assert block.forwards() == "DEFINE FIELD `sep` ON `t` TYPE string DEFAULT { LET $x = 1; RETURN $x; };"

    def test_r32_names_that_are_not_identifiers(self) -> None:
        assert DropTable("user-events").forwards() == "REMOVE TABLE `user-events`;"
        assert DropField("user-events", "first-name").forwards() == "REMOVE FIELD `first-name` ON `user-events`;"
        assert DropIndex("user-events", "idx-x").forwards() == "REMOVE INDEX `idx-x` ON `user-events`;"
        assert DropField("t", "a.b", path=("a.b",)).forwards() == "REMOVE FIELD `a.b` ON `t`;"

    def test_r33_line_2(self) -> None:
        assert R33.forwards(line=2, state=_r33_state()) == "\n".join(_R33_LINES)

    def test_r33_line_3_writes_the_table_statement_only(self) -> None:
        assert R33.forwards(line=3, state=_r33_state()) == _R33_LINES[0]
        assert R33.forwards() == _R33_LINES[0]
        assert R33.forwards(line=2) == _R33_LINES[0]  # no state: nothing to redefine

    def test_r33_skip_fields(self) -> None:
        assert R33.forwards(line=2, state=_r33_state(), skip_fields=["tags.*"]) == "\n".join(_R33_LINES[:3])

    def test_r33_backwards_restoring_schemafull(self) -> None:
        state = _r33_state()
        assert R33_REVERSE.backwards(line=2, state=state) == "\n".join(_R33_LINES)
        assert R33_REVERSE.forwards(line=2, state=state) == "DEFINE TABLE OVERWRITE `t` TYPE NORMAL SCHEMALESS;"
        assert R33_REVERSE.backwards(line=3, state=state) == _R33_LINES[0]
        assert R33.backwards(line=2, state=state) == "DEFINE TABLE OVERWRITE `t` TYPE NORMAL SCHEMALESS;"

    def test_d32_an_unknown_previous_mode_writes_the_redefinitions(self) -> None:
        unknown = AlterTable(name="t", schema_mode="SCHEMAFULL", table_type="normal")
        assert unknown.forwards(line=2, state=_r33_state()) == "\n".join(_R33_LINES)
        already = AlterTable(name="t", schema_mode="SCHEMAFULL", table_type="normal", previous_schema_mode="SCHEMAFULL")
        assert already.forwards(line=2, state=_r33_state()) == _R33_LINES[0]

    def test_ac2_a_migration_skips_the_fields_it_defines(self) -> None:
        before = _replay(
            CreateTable(name="t", schema_mode="SCHEMALESS", table_type="normal"),
            AddField("t", "name", "string"),
            AddField("t", "bag", "any"),
            AddField("t", "tags", "array"),
            AddField("t", "tags.*", "any"),
        )
        migration = Migration(name="0001_x", operations=[R33, AddField("t", "bag2", "any")])
        statements = migration.forwards_sql(line=2, state=before)
        assert statements == ["\n".join(_R33_LINES), "DEFINE FIELD `bag2` ON `t` TYPE any FLEXIBLE;"]
        assert migration.forwards_sql(state=before) == [_R33_LINES[0], "DEFINE FIELD `bag2` ON `t` TYPE any;"]

    def test_a_raw_sql_s_state_operations_count_as_the_migration_s_own_fields(self) -> None:
        before = _replay(CreateTable(name="t", schema_mode="SCHEMALESS", table_type="normal"), AddField("t", "bag", "any"))
        raw = RawSQL(
            "DEFINE FIELD OVERWRITE bag ON t TYPE any FLEXIBLE;", state_operations=[AddField("t", "bag", "any", flexible=True)]
        )
        migration = Migration(name="0001_x", operations=[R33, raw])
        assert migration.forwards_sql(line=2, state=before) == [_R33_LINES[0], raw.sql]


class TestMigrationSql:
    def test_forwards_sql_from_an_empty_state(self) -> None:
        migration = Migration(name="0001_x", operations=[CreateTable(name="t"), AddField("t", "bag", "any")])
        assert migration.forwards_sql(line=2) == [
            "DEFINE TABLE `t` SCHEMAFULL;",
            "DEFINE FIELD `bag` ON `t` TYPE any FLEXIBLE;",
        ]
        assert migration.forwards_sql() == ["DEFINE TABLE `t` SCHEMAFULL;", "DEFINE FIELD `bag` ON `t` TYPE any;"]

    def test_forwards_sql_leaves_out_the_empty_statements(self) -> None:
        data = DataMigration(forwards_func=_noop)
        migration = Migration(name="0001_x", operations=[R2, data, R6])
        assert migration.forwards_sql() == [R2.forwards(), R6.forwards()]

    def test_backwards_sql_in_reverse_order_reversible_only(self) -> None:
        migration = Migration(name="0001_x", operations=[R2, R12, R6])
        assert migration.backwards_sql() == ["REMOVE FIELD IF EXISTS `age` ON `person`;", "REMOVE TABLE IF EXISTS `log`;"]

    def test_backwards_sql_reads_the_state_before_the_migration(self) -> None:
        migration = Migration(name="0001_x", operations=[R33_REVERSE, AddField("t", "bag2", "any")])
        state = _r33_state()
        assert migration.backwards_sql(line=2, state=state) == [
            "REMOVE FIELD IF EXISTS `bag2` ON `t`;",
            "\n".join(_R33_LINES),
        ]

    def test_the_line_is_checked(self) -> None:
        with pytest.raises(ValueError, match="line= must be 2 or 3"):
            Migration(name="0001_x").forwards_sql(line=4)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="line= must be 2 or 3"):
            Migration(name="0001_x").backwards_sql(line=1)  # type: ignore[arg-type]

    def test_statements_give_each_operation_s_index(self) -> None:
        migration = Migration(name="0001_x", operations=[R2, DataMigration(forwards_func=_noop), R6, R12])
        assert _statements(migration, line=3, state=None) == [(0, R2.forwards()), (2, R6.forwards()), (3, R12.forwards())]
        assert _statements(migration, line=3, state=None, backwards=True) == [(2, R6.backwards()), (0, R2.backwards())]


async def _noop() -> None:
    """A data migration's function (never run by these tests)."""


class TestGoldenEqualities:
    """Spec §5.4: for the same arguments, an operation's statement is the applier's (``OVERWRITE`` removed)."""

    def test_define_event(self) -> None:
        event = Event("audit", when="$event = 'CREATE'", then="CREATE log SET who = $after.name", comment="c")
        operation = DefineEvent("audit", "person", "$event = 'CREATE'", "CREATE log SET who = $after.name", comment="c")
        assert operation.forwards() == render.plain(event.ddl("person"))
        assert dataclasses.replace(operation, overwrite=True).forwards() == event.ddl("person")
        asynchronous = Event("audit", then="CREATE log SET a = 1", async_mode=True, retry=2, max_depth=4)
        assert DefineEvent(
            "audit", "person", None, "CREATE log SET a = 1", async_mode=True, retry=2, max_depth=4
        ).forwards() == (render.plain(asynchronous.ddl("person")))

    def test_define_function(self) -> None:
        arguments: dict[str, Any] = {
            "parameters": {"n": "string", "times": "option<int>"},
            "returns": "string",
            "permissions": "WHERE $auth.admin = true",
            "comment": "Greets",
        }
        operation = DefineFunction("greet", "RETURN 'hi ' + $n;", **arguments)
        assert operation.forwards() == render.plain(function_ddl("greet", "RETURN 'hi ' + $n;", overwrite=True, **arguments))
        assert operation.name == "fn::greet"

    @pytest.mark.parametrize("line", [2, 3])
    def test_create_index(self, line: Literal[2, 3]) -> None:
        cases = [
            (R13, Index("person_email", "email", unique=True)),
            (R14, Index("post_body", "body", search_analyzer="english", bm25=True, highlights=True)),
            (R15, Index("doc_emb", "emb", hnsw=True, dimension=3, dist="COSINE")),
            (
                CreateIndex(table="t", name="ab", fields=["a", "b.c"], comment="two", search_analyzer=None),
                Index("ab", ["a", "b.c"], comment="two"),
            ),
            (
                CreateIndex(table="t", name="ft", fields=["x"], search_analyzer="en", bm25=(1.5, 0.5)),
                Index("ft", "x", search_analyzer="en", bm25=(1.5, 0.5)),
            ),
        ]
        for operation, index in cases:
            assert operation.forwards(line=line) == render.plain(index.ddl(operation.table, line=line))

    def test_define_analyzer(self) -> None:
        analyzer = Analyzer("fts", ["blank"], ["ascii", "edgengram(2,10)"], function="low", comment="c")
        operation = DefineAnalyzer("fts", ["blank"], ["ascii", "edgengram(2,10)"], function="low", comment="c")
        assert operation.forwards() == analyzer.ddl(overwrite=True)
        assert operation.function == "fn::low"


class TestOperationChecks:
    """Spec §5.3: what construction refuses, before any request."""

    @pytest.mark.parametrize(
        "build",
        [
            lambda: CreateTable(name="user-events"),
            lambda: AlterTable(name="2024_sales"),
            lambda: AddField(table="user-events", name="a", field_type="int"),
            lambda: AlterField(table="user-events", name="a"),
            lambda: CreateIndex(table="t", name="idx-x", fields=["a"]),
            lambda: CreateIndex(table="t-x", name="i", fields=["a"]),
            lambda: RebuildIndex("t", "idx-x"),
            lambda: DefineAnalyzer("an-1"),
            lambda: DefineEvent("audit-x", "person", None, "CREATE a"),
            lambda: DefineEvent("audit", "per son", None, "CREATE a"),
            lambda: DefineAccess("acc-x", "users", {"email": "$email"}, "email = $email"),
            lambda: DefineAccess("account", "user s", {"email": "$email"}, "email = $email"),
            lambda: DefineFunction("fn::a-b", "RETURN 1;"),
            lambda: RemoveFunction("fn::a b"),
        ],
    )
    def test_a_definition_name_must_be_an_identifier(self, build: Any) -> None:
        with pytest.raises(ValueError):
            build()

    def test_the_message_names_the_operation(self) -> None:
        with pytest.raises(ValueError, match=r"^CreateTable: Invalid table name 'user-events'"):
            CreateTable(name="user-events")
        with pytest.raises(TypeError, match=r"^CreateTable: Table name must be a str, got int"):
            CreateTable(name=1)  # type: ignore[arg-type]

    def test_a_kind_the_parser_cannot_read(self) -> None:
        with pytest.raises(ValueError, match=r"^AddField 'a' on 'person': field_type= is not a SurrealQL kind"):
            AddField("person", "a", "frob<")
        with pytest.raises(ValueError, match="previous_type= is not a SurrealQL kind"):
            AlterField("person", "a", "int", previous_type="frob<")
        with pytest.raises(TypeError, match="field_type= must be a str"):
            AddField("person", "a", 1)  # type: ignore[arg-type]

    def test_a_kind_is_kept_in_its_canonical_form(self) -> None:
        assert AddField("person", "a", "none | int").field_type == "option<int>"
        assert AddField("person", "a", "array< string >").field_type == "array<string>"

    @pytest.mark.parametrize("default", [object(), {object()}, 2**70, "a\x00b"])
    def test_a_default_without_a_literal_form(self, default: object) -> None:
        with pytest.raises(TypeError, match=r"^AddField 'a' on 'person': default= has no SurrealQL literal form"):
            AddField("person", "a", "any", default=default)
        with pytest.raises(TypeError, match="previous_default= has no SurrealQL literal form"):
            AlterField("person", "a", "any", previous_type="any", previous_default=default)

    @pytest.mark.parametrize(
        "default", [18, 1.5, True, "a;b", Decimal("1.10"), datetime(2026, 1, 1, tzinfo=UTC), [1, 2], {"a": 1}, None]
    )
    def test_a_default_with_a_literal_form(self, default: object) -> None:
        assert AddField("person", "a", "any", default=default).default == default

    def test_an_expression_default(self) -> None:
        assert (
            AddField("person", "a", "datetime", default=SurrealFunc("time::now()")).forwards().endswith("DEFAULT time::now();")
        )
        assert AddField("person", "a", "int", default=DefaultExpression("1 + 1")).forwards().endswith("DEFAULT 1 + 1;")

    def test_on_delete_needs_reference(self) -> None:
        with pytest.raises(ValueError, match=r"on_delete= needs reference=True"):
            AddField("person", "owner", "option<record<user>>", on_delete="CASCADE")
        assert (
            AddField("person", "owner", "option<record<user>>", reference=True, on_delete="cascade").forwards()
            == "DEFINE FIELD `owner` ON `person` TYPE option<record<user>> REFERENCE ON DELETE CASCADE;"
        )
        then = AddField("person", "owner", "option<record<user>>", reference=True, on_delete="THEN UPDATE x SET y = 1")
        assert then.forwards().endswith("REFERENCE ON DELETE THEN UPDATE x SET y = 1;")

    @pytest.mark.parametrize("on_delete", ["DROP", "THEN", "THEN a; b", 1])
    def test_an_unknown_on_delete(self, on_delete: Any) -> None:
        with pytest.raises((ValueError, TypeError), match="on_delete="):
            AddField("person", "owner", "option<record<user>>", reference=True, on_delete=on_delete)

    @pytest.mark.parametrize(
        "keywords",
        [
            {"default": 1},
            {"value": "a + 2"},
            {"readonly": True},
            {"assertion": "$value > 0"},
            {"reference": True},
        ],
    )
    def test_computed_refuses_the_value_clauses(self, keywords: dict[str, Any]) -> None:
        with pytest.raises(ValueError, match="computed= cannot be combined with"):
            AddField("t", "c", "int", computed="a + 1", **keywords)
        previous = {f"previous_{key}": value for key, value in keywords.items()}
        with pytest.raises(ValueError, match="previous_computed= cannot be combined with"):
            AlterField("t", "c", "int", previous_type="int", previous_computed="a + 1", **previous)

    def test_default_always_needs_a_default(self) -> None:
        with pytest.raises(ValueError, match=r"default_always=True needs default="):
            AddField("t", "d", "int", default_always=True)
        with pytest.raises(ValueError, match=r"previous_default_always=True needs previous_default="):
            AlterField("t", "d", "int", previous_type="int", previous_default_always=True)

    @pytest.mark.parametrize(
        ("argument", "value"),
        [
            ("assertion", "$value > 0; DELETE t"),
            ("value", "a -- comment"),
            ("computed", "(a + 1"),
            ("assertion", ""),
        ],
    )
    def test_an_expression_is_one_expression(self, argument: str, value: str) -> None:
        with pytest.raises(ValueError, match=f"{argument}="):
            AddField("t", "c", "int", **{argument: value})

    @pytest.mark.parametrize(
        "permissions",
        [{"delete": "FULL"}, {"select": "SOMETIMES"}, {"select": "WHERE a; b"}, {"select": 1}, ["select"]],
    )
    def test_field_permissions(self, permissions: Any) -> None:
        with pytest.raises((ValueError, TypeError), match="permissions="):
            AddField("t", "c", "int", permissions=permissions)

    def test_field_permissions_are_normalised(self) -> None:
        field = AddField("t", "c", "int", permissions={"select": "full", "update": "where $auth.id = id"})
        assert field.permissions == {"select": "FULL", "update": "WHERE $auth.id = id"}

    def test_relation_keys_need_a_relation_table(self) -> None:
        with pytest.raises(ValueError, match="relation_in=, relation_out= and enforced= need table_type='relation'"):
            CreateTable(name="likes", relation_in="person")
        with pytest.raises(ValueError, match="need table_type='relation'"):
            AlterTable(name="likes", table_type="normal", enforced=True)
        with pytest.raises(ValueError, match="previous_relation_in=, previous_relation_out= and previous_enforced="):
            AlterTable(name="likes", previous_schema_mode="SCHEMALESS", previous_relation_out=["a"])

    def test_a_view_takes_no_table_type(self) -> None:
        with pytest.raises(ValueError, match="view_query= takes no table_type= and no relation key"):
            CreateTable(name="v", table_type="normal", view_query="SELECT * FROM person")
        with pytest.raises(ValueError, match="view_query= takes no table_type= and no relation key"):
            CreateTable(name="v", table_type="relation", relation_in="a", view_query="SELECT * FROM person")

    @pytest.mark.parametrize(
        ("keywords", "message"),
        [
            ({"schema_mode": "STRICT"}, "schema_mode= must be"),
            ({"schema_mode": 1}, "schema_mode= must be"),
            ({"table_type": "user"}, "table_type= must be"),
            ({"table_type": 1}, "table_type= must be"),
            ({"changefeed": "7x"}, "changefeed= is '7x', not a change feed"),
            ({"permissions": {"drop": "FULL"}}, "permissions= names the operation 'drop'"),
            ({"permissions": "FULL"}, "permissions= must be a mapping"),
            ({"comment": "a\x00b"}, "comment= holds a NUL"),
            ({"view_query": "DELETE person"}, "view_query= must be a SELECT"),
            ({"relation_in": "a-b", "table_type": "relation"}, "relation_in= names 'a-b'"),
            ({"enforced": "yes", "table_type": "relation"}, "enforced= must be a bool"),
            ({"drop": 1}, "drop= must be a bool"),
        ],
    )
    def test_table_keys(self, keywords: dict[str, Any], message: str) -> None:
        with pytest.raises((ValueError, TypeError), match=re.escape(message)):
            CreateTable(name="t", **keywords)

    def test_table_keys_are_normalised(self) -> None:
        table = CreateTable(
            name="t",
            schema_mode=SchemaMode.SCHEMALESS,
            table_type=TableType.USER,
            changefeed="7d  include original",
            permissions={"select": "full"},
        )
        assert (table.schema_mode, table.table_type, table.changefeed) == ("SCHEMALESS", "normal", "7d INCLUDE ORIGINAL")
        assert table.permissions == {"select": "FULL", "create": "NONE", "update": "NONE", "delete": "NONE"}
        assert CreateTable(name="t", schema_mode="schemaless", table_type="ANY").table_type == "any"
        assert CreateTable(name="t", table_type=TableType.ANY).table_type == "any"
        relation = CreateTable(name="r", table_type=TableType.RELATION, relation_in="a | b", relation_out=("c",))
        assert (relation.relation_in, relation.relation_out) == (("a", "b"), ("c",))

    def test_concurrently(self) -> None:
        with pytest.raises(ValueError) as raised:
            CreateIndex(table="t", name="i", fields=["a"], concurrently=True)
        assert str(raised.value) == (
            "CreateIndex(concurrently=True) is not supported yet: an index is built inside its migration's transaction."
        )

    @pytest.mark.parametrize(
        "keywords",
        [
            {"fields": []},
            {"fields": ["a", "b"], "search_analyzer": "en"},
            {"fields": ["a"], "unique": True, "search_analyzer": "en"},
            {"fields": ["a"], "bm25": True},
            {"fields": ["a"], "hnsw": True},
            {"fields": ["a"], "dimension": 3},
            {"fields": ["a b"]},
            {"fields": ["a"], "search_analyzer": "en-x"},
        ],
    )
    def test_index_arguments(self, keywords: dict[str, Any]) -> None:
        with pytest.raises(ValueError, match=r"^CreateIndex 'i' on 't': (Index 'i'|Invalid analyzer name)"):
            CreateIndex(table="t", name="i", **keywords)

    def test_index_arguments_are_normalised(self) -> None:
        index = CreateIndex(table="t", name="v", fields="e", hnsw=True, dimension=3, dist="cosine", vector_type="f64")
        assert (index.fields, index.dist, index.vector_type) == (("e",), "COSINE", "F64")
        assert CreateIndex(table="t", name="f", fields=["a"], search_analyzer="en", bm25=(1, 0)).bm25 == (1.0, 0.0)
        assert CreateIndex(table="t", name="f", fields=["a"], search_analyzer="en", bm25=False).bm25 is None

    def test_analyzer_arguments(self) -> None:
        with pytest.raises(ValueError, match=r"^DefineAnalyzer 'a': Analyzer 'a': unknown tokenizer"):
            DefineAnalyzer("a", ["word"])
        with pytest.raises(ValueError, match=r"needs 1 <= min <= max"):
            DefineAnalyzer("a", ["blank"], ["edgengram(10,2)"])
        analyzer = DefineAnalyzer("a", ["BLANK"], ["Snowball(English)"])
        assert (analyzer.tokenizers, analyzer.filters) == (("blank",), ("snowball(english)",))

    def test_event_arguments(self) -> None:
        with pytest.raises(ValueError, match=r"^DefineEvent 'audit' on 'person': Event 'audit': then= is empty"):
            DefineEvent("audit", "person", None, " ")
        with pytest.raises(ValueError, match="retry= and max_depth= apply to ASYNC events only"):
            DefineEvent("audit", "person", None, "CREATE a", retry=1)
        with pytest.raises(ValueError, match="when= is one expression"):
            DefineEvent("audit", "person", "a; b", "CREATE a")
        assert DefineEvent("audit", "person", " $event = 'CREATE' ", "  CREATE a  ").then == "CREATE a"

    @pytest.mark.parametrize(
        ("keywords", "message"),
        [
            ({"signup_fields": {}}, "signup_fields= sets no column"),
            ({"signup_fields": ["email"]}, "signup_fields= must be a mapping"),
            ({"signup_fields": {"e mail": "$email"}}, "Invalid signup_fields column name 'e mail'"),
            ({"signup_fields": {"email": "$a; DELETE users"}}, "the expression of signup_fields['email'] is one expression"),
            ({"signup_fields": {"email": 1}}, "the expression of signup_fields['email'] must be a str"),
            ({"signin_where": "a -- b"}, "signin_where= is not SurrealQL the ORM can embed"),
            ({"duration_token": "15 minutes"}, "duration_token= is '15 minutes', not a SurrealQL duration"),
            ({"duration_session": None}, "duration_session= must be a str"),
            ({"duration_grant": "x"}, "duration_grant= is 'x', not a SurrealQL duration"),
            ({"with_refresh": 1}, "with_refresh= must be a bool"),
            ({"comment": 1}, "comment= must be a str"),
        ],
    )
    def test_access_arguments(self, keywords: dict[str, Any], message: str) -> None:
        arguments: dict[str, Any] = {
            "name": "account",
            "table": "users",
            "signup_fields": {"email": "$email"},
            "signin_where": "email = $email",
        }
        arguments.update(keywords)
        with pytest.raises((ValueError, TypeError), match=re.escape(message)):
            DefineAccess(**arguments)

    def test_access_durations(self) -> None:
        access = DefineAccess("a", "users", {"email": "$email"}, "email = $email", "1h30m", "NONE", with_refresh=True)
        assert access.forwards().endswith("WITH REFRESH DURATION FOR TOKEN 1h30m, FOR SESSION NONE, FOR GRANT 30d;")
        granted = dataclasses.replace(access, duration_grant="7d", comment="c")
        assert granted.forwards().endswith("FOR GRANT 7d COMMENT 'c';")

    def test_function_arguments(self) -> None:
        with pytest.raises(ValueError, match=r"^DefineFunction 'fn::greet': define_function\('fn::greet'\): body= is empty"):
            DefineFunction("greet", " ")
        with pytest.raises(ValueError, match="returns= is not a SurrealQL kind"):
            DefineFunction("greet", "RETURN 1;", returns="frob<")
        with pytest.raises(ValueError, match="is one SurrealDB defines itself"):
            DefineFunction("greet", "RETURN 1;", parameters={"auth": "string"})

    @pytest.mark.parametrize(
        ("build", "message"),
        [
            (lambda: CreateIndex("t", "i", ["a"], previous=DropIndex("t", "i")), "previous= must be a CreateIndex"),
            (lambda: CreateIndex("t", "i", ["a"], previous=CreateIndex("t", "j", ["a"])), "previous= names index 'j'"),
            (lambda: CreateIndex("t", "i", ["a"], previous=CreateIndex("u", "i", ["a"])), "previous= is on table 'u'"),
            (
                lambda: CreateIndex("t", "i", ["a"], previous=CreateIndex("t", "i", ["a"], overwrite=True)),
                "previous= must be a creation",
            ),
            (
                lambda: CreateIndex(
                    "t", "i", ["a"], previous=CreateIndex("t", "i", ["b"], previous=CreateIndex("t", "i", ["a"]))
                ),
                "previous= must be a creation",
            ),
            (lambda: DropIndex("t", "i", previous=DropIndex("t", "i")), "previous= must be a CreateIndex"),
            (lambda: RemoveAnalyzer("a", previous=DefineAnalyzer("b")), "previous= names analyzer 'b'"),
            (
                lambda: RemoveAnalyzer("a", previous=DefineAnalyzer("a", previous=DefineAnalyzer("a"))),
                "previous= must be a creation",
            ),
            (lambda: RemoveEvent("e", "t", previous=DefineEvent("e", "u", None, "CREATE a")), "previous= is on table 'u'"),
            (
                lambda: DefineEvent("e", "t", None, "CREATE a", previous=RemoveEvent("e", "t")),
                "previous= must be a DefineEvent",
            ),
            (lambda: RemoveAccess("a", previous=DefineAccess("b", "u", {"e": "$e"}, "e = $e")), "previous= names access 'b'"),
            (lambda: RemoveFunction("fn::a", previous=DefineFunction("b", "RETURN 1;")), "previous= names function 'fn::b'"),
            (lambda: DefineFunction("a", "RETURN 1;", previous="RETURN 2;"), "previous= must be a DefineFunction"),
        ],
    )
    def test_previous(self, build: Any, message: str) -> None:
        with pytest.raises(ValueError, match=re.escape(message)):
            build()

    def test_a_previous_function_name_is_normalised(self) -> None:
        assert RemoveFunction("greet", previous=R22).name == "fn::greet"
        assert DefineFunction("fn::greet", "RETURN 2;", previous=DefineFunction("greet", "RETURN 1;")).reversible

    def test_data_migration(self) -> None:
        with pytest.raises(ValueError) as raised:
            DataMigration()
        assert str(raised.value) == "DataMigration requires either forwards_sql or forwards_func"
        with pytest.raises(ValueError, match="^DataMigration: BEGIN, COMMIT, CANCEL and USE are not allowed"):
            DataMigration(forwards_sql="BEGIN; UPDATE a SET b = 1;")
        with pytest.raises(ValueError, match="^DataMigration: BEGIN, COMMIT, CANCEL and USE are not allowed"):
            DataMigration(forwards_sql="UPDATE a SET b = 1;", backwards_sql="COMMIT;")
        with pytest.raises(TypeError, match="forwards_func= must be an async function taking no argument"):
            DataMigration(forwards_func=lambda: None)  # type: ignore[arg-type,return-value]
        with pytest.raises(TypeError, match="backwards_func= must be an async function taking no argument"):
            DataMigration(forwards_func=_noop, backwards_func=_takes_one)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="description= must be a str"):
            DataMigration(forwards_func=_noop, description=1)  # type: ignore[arg-type]
        assert DataMigration(forwards_func=_noop).has_func

    def test_raw_sql(self) -> None:
        with pytest.raises(ValueError) as raised:
            RawSQL("BEGIN TRANSACTION; UPDATE a SET b = 1; COMMIT TRANSACTION;")
        assert str(raised.value) == (
            "RawSQL: BEGIN, COMMIT, CANCEL and USE are not allowed; each migration runs in a transaction of its own."
        )
        with pytest.raises(ValueError, match="RawSQL: sql= holds no statement"):
            RawSQL("  ;  ")
        with pytest.raises(ValueError, match="RawSQL: reverse_sql= holds no statement"):
            RawSQL("UPDATE a SET b = 1;", reverse_sql=" ")
        with pytest.raises(ValueError, match="RawSQL: BEGIN, COMMIT, CANCEL and USE"):
            RawSQL("UPDATE a SET b = 1;", reverse_sql="USE NS other;")
        with pytest.raises(TypeError, match="RawSQL: state_operations= item 1 is not an Operation"):
            RawSQL("UPDATE a SET b = 1;", state_operations=["DEFINE TABLE t"])  # type: ignore[list-item]
        with pytest.raises(TypeError, match="state_operations= must be a list or tuple"):
            RawSQL("UPDATE a SET b = 1;", state_operations=R2)  # type: ignore[arg-type]
        assert RawSQL("UPDATE a SET b = 1;", state_operations=[R2]).state_operations == (R2,)

    @pytest.mark.parametrize("operation", [R1, R5, R6, R11, R12, R13, R17, R18, R20, R22, R24, RawSQL("UPDATE a SET b = 1;")])
    def test_the_line_is_checked(self, operation: Operation) -> None:
        with pytest.raises(ValueError, match="line= must be 2 or 3"):
            operation.forwards(line=4)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="line= must be 2 or 3"):
            operation.backwards(line=1)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="line= must be 2 or 3"):
            operation.forwards(line=True)  # type: ignore[arg-type]

    def test_a_bool_flag(self) -> None:
        with pytest.raises(TypeError, match="overwrite= must be a bool"):
            AddField("t", "a", "int", overwrite=1)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="if_exists= must be a bool"):
            RebuildIndex("t", "i", if_exists="yes")  # type: ignore[arg-type]

    def test_alter_field_without_a_type_writes_none(self) -> None:
        bare = AlterField("t", "a", default=1)
        assert bare.forwards() == "DEFINE FIELD OVERWRITE `a` ON `t` DEFAULT 1;"
        state = CreateTable(name="t").state_forwards(SchemaState())
        assert AlterField("t", "a").forwards(line=2, state=state) == "DEFINE FIELD OVERWRITE `a` ON `t` FLEXIBLE;"
        assert render.read_own(bare.forwards()) == FieldState(
            "a", "any", False, "1", table="t", raw="DEFINE FIELD `a` ON `t` DEFAULT 1"
        )


async def _takes_one(value: int) -> None:
    """Not a data migration's function: it takes an argument."""


class TestNames:
    """D26: removals take any name a database holds; definitions keep the identifier rule."""

    def test_a_removal_takes_any_name(self) -> None:
        assert DropTable("user-events").name == "user-events"
        assert DropTable("2024_sales").forwards() == "REMOVE TABLE `2024_sales`;"
        assert RemoveAnalyzer("odd name").forwards() == "REMOVE ANALYZER `odd name`;"
        assert RemoveEvent("e-1", "t-1").forwards() == "REMOVE EVENT `e-1` ON `t-1`;"
        assert RemoveAccess("a`b").forwards() == "REMOVE ACCESS `a\\`b` ON DATABASE;"

    def test_a_definition_refuses_it(self) -> None:
        with pytest.raises(ValueError, match="Invalid table name 'user-events'"):
            CreateTable(name="user-events")

    @pytest.mark.parametrize(
        "build",
        [
            lambda: DropTable("a\x00b"),
            lambda: DropTable(""),
            lambda: DropField("t\x00", "a"),
            lambda: DropField("t", "a\x00"),
            lambda: DropField("t", "a..b"),
            lambda: DropIndex("t", "i\x00"),
            lambda: RemoveAnalyzer("a\x00"),
            lambda: RemoveEvent("e\x00", "t"),
            lambda: RemoveAccess("a\x00"),
            lambda: RemoveFunction("fn::a\x00"),
            lambda: AddField("t", "a\x00", "int"),
            lambda: AlterField("t", "a", path=("a\x00",)),
            lambda: CreateTable(name="a\x00"),
            lambda: CreateIndex("t", "i\x00", ["a"]),
            lambda: DefineEvent("e\x00", "t", None, "CREATE a"),
            lambda: DefineAccess("a\x00", "u", {"e": "$e"}, "e = $e"),
            lambda: DefineAnalyzer("a\x00"),
            lambda: DefineFunction("fn::a\x00", "RETURN 1;"),
            lambda: RebuildIndex("t", "i\x00"),
        ],
    )
    def test_a_nul_is_refused_everywhere(self, build: Any) -> None:
        with pytest.raises(ValueError):
            build()

    def test_a_name_of_another_type(self) -> None:
        with pytest.raises(TypeError, match="DropTable: name= must be a str, got int"):
            DropTable(1)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="AddField 'a' on 't': path= takes a sequence of str, not a str"):
            AddField("t", "a", "int", path="a")

    def test_a_segment_holding_a_dot(self) -> None:
        field = AddField("t", "a.b", "int", path=("a.b",))
        assert field.path == ("a.b",)
        assert field.forwards() == "DEFINE FIELD `a.b` ON `t` TYPE int;"
        assert field.describe() == "Add field 'a.b' to t"
        with pytest.raises(ValueError, match=r"name= 'a\.c' is not its path joined with '\.'"):
            AddField("t", "a.c", "int", path=("a", "b"))

    def test_the_path_is_derived_from_the_name(self) -> None:
        assert AddField("t", "tags.*", "any").path == ("tags", "*")
        assert AddField("t", "tags.*", "any").forwards() == "DEFINE FIELD `tags`.* ON `t` TYPE any;"

    def test_describe_quotes_a_name_that_is_not_an_identifier(self) -> None:
        assert DropTable("user-events").describe() == "Drop table 'user-events'"
        assert DropField("user-events", "first-name").describe() == "Drop field 'first-name' from 'user-events'"
        assert DropIndex("t", "idx\nx").describe() == "Drop index 'idx\\nx' from t"
        assert DropField("t", "address.city").describe() == "Drop field address.city from t"

    def test_a_full_orm_positional_call_reaching_encrypted(self) -> None:
        with pytest.raises(TypeError):
            AddField("t", "f", "int", None, None, False)  # type: ignore[misc]
        with pytest.raises(TypeError):
            AlterField("t", "f", "int", None, None, False)  # type: ignore[misc]


_REVERSIBILITY: list[tuple[Operation, bool, bool]] = [
    (R1, True, False),
    (R5, True, False),
    (AlterTable(name="t", schema_mode="SCHEMAFULL"), False, False),
    (DropTable("t"), False, True),
    (R6, True, False),
    (AddField("t", "a", "int", overwrite=True), True, False),
    (R11, True, False),
    (AlterField("t", "a", "int"), False, False),
    (R12, False, True),
    (R13, True, False),
    (R16, True, False),
    (CreateIndex("t", "i", ["a"], overwrite=True), False, False),
    (DropIndex("t", "i"), False, True),
    (DropIndex("person", "person_email", previous=R13), True, True),
    (R24, True, False),
    (R17, True, False),
    (RemoveAnalyzer("english"), False, True),
    (RemoveAnalyzer("english", previous=R17), True, True),
    (R18, True, False),
    (DefineEvent("audit", "person", None, "CREATE a", overwrite=True), False, False),
    (RemoveEvent("audit", "person"), False, True),
    (RemoveEvent("audit", "person", previous=R18), True, True),
    (R20, True, False),
    (DefineAccess("a", "u", {"e": "$e"}, "e = $e", overwrite=True), False, False),
    (RemoveAccess("account"), False, True),
    (RemoveAccess("account", previous=R20), True, True),
    (R22, True, False),
    (DefineFunction("greet", "RETURN 1;", overwrite=True), False, False),
    (RemoveFunction("greet"), False, True),
    (RemoveFunction("greet", previous=R22), True, True),
    (DataMigration(forwards_sql="UPDATE a SET b = 1;"), False, False),
    (DataMigration(forwards_sql="UPDATE a SET b = 1;", backwards_sql="UPDATE a SET b = 0;"), True, False),
    (DataMigration(forwards_func=_noop, backwards_func=_noop), True, False),
    (RawSQL("UPDATE a SET b = 1;"), False, False),
    (RawSQL("UPDATE a SET b = 1;", reverse_sql="UPDATE a SET b = 0;"), True, False),
    (RawSQL("REMOVE TABLE t;", reverse_sql="DEFINE TABLE t;", state_operations=[DropTable("t")]), False, False),
    (
        RawSQL("DEFINE FIELD a ON t;", reverse_sql="REMOVE FIELD a ON t;", state_operations=[AddField("t", "a", "any")]),
        True,
        False,
    ),
]


class TestReversible:
    @pytest.mark.parametrize(("operation", "reversible", "destructive"), _REVERSIBILITY)
    def test_the_table(self, operation: Operation, reversible: bool, destructive: bool) -> None:
        assert operation.reversible is reversible
        assert operation.destructive is destructive

    @pytest.mark.parametrize(("operation", "reversible", "destructive"), _REVERSIBILITY)
    def test_an_irreversible_backwards_is_empty(self, operation: Operation, reversible: bool, destructive: bool) -> None:
        if not reversible:
            assert operation.backwards() == ""
            assert operation.backwards(line=2) == ""

    def test_reversible_is_read_only(self) -> None:
        with pytest.raises(AttributeError):
            R6.reversible = False  # type: ignore[misc]
        with pytest.raises(dataclasses.FrozenInstanceError):
            R6.name = "other"  # type: ignore[misc]

    def test_a_previous_makes_a_redefinition_overwrite(self) -> None:
        assert DefineEvent("audit", "person", None, "CREATE a", previous=R18).forwards().startswith("DEFINE EVENT OVERWRITE")
        assert (
            DefineAccess("account", "users", {"email": "$email"}, "email = $email", previous=R20)
            .forwards()
            .startswith("DEFINE ACCESS OVERWRITE account")
        )
        assert DefineFunction("greet", "RETURN 1;", previous=R22).forwards().startswith("DEFINE FUNCTION OVERWRITE")
        assert DefineAnalyzer("english", ["blank"], previous=R17).backwards() == R17.forwards()

    def test_a_redefinition_s_backwards_restores_previous_with_overwrite(self) -> None:
        event = DefineEvent("audit", "person", None, "CREATE a", previous=R18)
        assert event.backwards() == _R18_TEXT.replace("DEFINE EVENT", "DEFINE EVENT OVERWRITE")
        access = DefineAccess("account", "users", {"email": "$email"}, "email = $email", previous=R20)
        assert access.backwards() == _R20_TEXT.replace("DEFINE ACCESS", "DEFINE ACCESS OVERWRITE")
        function = DefineFunction("greet", "RETURN 1;", previous=R22)
        assert function.backwards() == _R22_TEXT.replace("DEFINE FUNCTION", "DEFINE FUNCTION OVERWRITE")
        index = CreateIndex(
            "post",
            "post_body",
            ["body"],
            search_analyzer="english",
            previous=CreateIndex("post", "post_body", ["body"], search_analyzer="english", bm25=True),
        )
        assert (
            index.backwards(line=2)
            == "DEFINE INDEX OVERWRITE `post_body` ON `post` FIELDS `body` SEARCH ANALYZER `english` BM25;"
        )

    def test_the_overwrite_forms(self) -> None:
        assert AddField("t", "a", "int", overwrite=True).forwards() == "DEFINE FIELD OVERWRITE `a` ON `t` TYPE int;"
        assert CreateIndex("t", "i", ["a"], overwrite=True).forwards() == "DEFINE INDEX OVERWRITE `i` ON `t` FIELDS `a`;"
        assert DefineEvent("e", "t", None, "CREATE a", overwrite=True).forwards() == (
            "DEFINE EVENT OVERWRITE `e` ON `t` THEN { CREATE a };"
        )


_DESCRIPTIONS: list[tuple[Operation, str]] = [
    (R1, "Create table person"),
    (R5, "Alter table person"),
    (DropTable("log"), "Drop table log"),
    (R6, "Add field age to person"),
    (R11, "Alter field age on person"),
    (R12, "Drop field age from person"),
    (R13, "Create index person_email on person"),
    (DropIndex("person", "person_email"), "Drop index person_email from person"),
    (R24, "Rebuild index post_body on post"),
    (R17, "Define analyzer english"),
    (RemoveAnalyzer("english"), "Remove analyzer english"),
    (R18, "Define event audit on person"),
    (RemoveEvent("audit", "person"), "Remove event audit from person"),
    (R20, "Define access account for users"),
    (RemoveAccess("account"), "Remove access account"),
    (R22, "Define function fn::greet"),
    (RemoveFunction("greet"), "Remove function fn::greet"),
    (DataMigration(forwards_sql="UPDATE a SET b = 1;"), "Data migration"),
    (DataMigration(forwards_sql="UPDATE a SET b = 1;", description="Fill b"), "Fill b"),
    (RawSQL("UPDATE a SET b = 1;"), "Raw SQL"),
    (RawSQL("UPDATE a SET b = 1;", description="Fix b"), "Fix b"),
]


@pytest.mark.parametrize(("operation", "text"), _DESCRIPTIONS)
def test_describe(operation: Operation, text: str) -> None:
    assert operation.describe() == text


def test_migration_describe_lists_the_operations() -> None:
    migration = Migration(name="0002_b", dependencies=["0001_a"], operations=[R2, R6])
    assert migration.describe() == (
        "Migration: 0002_b\nDependencies: 0001_a\nOperations (2):\n  1. Create table log\n  2. Add field age to person"
    )


def _person(*operations: Operation) -> SchemaState:
    """A state holding ``person`` (SCHEMAFULL, ``People``) with ``age``, ``email``, ``person_email`` and ``audit``,
    then *operations*."""
    return _replay(
        CreateTable(name="person", schema_mode="SCHEMAFULL", comment="People"),
        R6,
        AddField("person", "email", "string"),
        AddField("person", "address", "object"),
        AddField("person", "address.city", "string"),
        R13,
        R18,
        *operations,
    )


class TestStateForwards:
    """Spec §6.2 (D31 as amended, C9; M28–M31): SurrealDB's DDL semantics, never an error for a missing definition."""

    def test_a_creation_over_an_existing_definition_replaces_it(self) -> None:
        state = _person(AddField("person", "age", "string"))
        assert state.tables["person"].fields["age"].field_type == "string"
        assert state.tables["person"].fields["age"].default is None
        index = _person(CreateIndex("person", "person_email", ["email", "age"])).tables["person"].indexes["person_email"]
        assert (index.fields, index.unique) == (("email", "age"), False)
        event = _person(DefineEvent("audit", "person", None, "CREATE b")).tables["person"].events["audit"]
        assert (event.when, event.then) == ("true", "{ CREATE b }")

    @pytest.mark.parametrize(
        "operation",
        [
            CreateTable(name="person", schema_mode="SCHEMALESS", comment="Others"),
            AlterTable(name="person", schema_mode="SCHEMALESS", comment="Others"),
        ],
    )
    def test_a_table_definition_keeps_the_fields_indexes_and_events(self, operation: Operation) -> None:
        before = _person()
        table = operation.state_forwards(before).tables["person"]
        assert (table.schema_mode, table.comment) == ("SCHEMALESS", "Others")
        assert table.fields == before.tables["person"].fields
        assert table.indexes == before.tables["person"].indexes
        assert table.events == before.tables["person"].events

    def test_an_alteration_of_a_missing_definition_places_it(self) -> None:
        table = AlterTable(name="t", schema_mode="SCHEMAFULL").state_forwards(SchemaState()).tables["t"]
        assert (table.schema_mode, table.table_type, dict(table.fields)) == ("SCHEMAFULL", "any", {})
        field = AlterField("person", "nick", "string").state_forwards(_person()).tables["person"].fields["nick"]
        assert (field.field_type, field.table) == ("string", "person")

    @pytest.mark.parametrize(
        ("operation", "kind", "name"),
        [
            (AddField("tf", "a", "int"), "fields", "a"),
            (AlterField("tf", "a", "int"), "fields", "a"),
            (CreateIndex("tf", "ix", ["a"]), "indexes", "ix"),
            (DefineEvent("ev", "tf", None, "CREATE a"), "events", "ev"),
        ],
    )
    def test_a_definition_on_a_missing_table_places_the_implicit_table(
        self, operation: Operation, kind: str, name: str
    ) -> None:
        table = operation.state_forwards(SchemaState()).tables["tf"]
        assert (table.schema_mode, table.table_type) == ("SCHEMALESS", "any")
        assert table.permissions == {}
        assert list(getattr(table, kind)) == [name]
        assert [key for key in ("fields", "indexes", "events") if getattr(table, key)] == [kind]

    def test_drop_field_of_a_parent_keeps_its_children(self) -> None:
        fields = DropField("person", "address").state_forwards(_person()).tables["person"].fields
        assert "address" not in fields
        assert "address.city" in fields

    def test_drop_table_takes_the_table_with_its_definitions(self) -> None:
        state = DropTable("person").state_forwards(_person(CreateTable(name="log")))
        assert list(state.tables) == ["log"]

    @pytest.mark.parametrize(
        "operation",
        [
            DropTable("nope"),
            DropField("person", "nope"),
            DropField("nope", "a"),
            DropIndex("person", "nope"),
            DropIndex("nope", "i"),
            RemoveEvent("nope", "person"),
            RemoveEvent("e", "nope"),
            RemoveAnalyzer("nope"),
            RemoveAccess("nope"),
            RemoveFunction("nope"),
        ],
    )
    def test_a_removal_of_a_missing_definition_leaves_the_state(self, operation: Operation) -> None:
        before = _person()
        assert operation.state_forwards(before) == before

    @pytest.mark.parametrize(
        ("operation", "removed"),
        [
            (DropIndex("person", "person_email"), lambda s: "person_email" in s.tables["person"].indexes),
            (RemoveEvent("audit", "person"), lambda s: "audit" in s.tables["person"].events),
            (RemoveAnalyzer("english"), lambda s: "english" in s.analyzers),
            (RemoveAccess("account"), lambda s: "account" in s.accesses),
            (RemoveFunction("greet"), lambda s: "fn::greet" in s.functions),
            (DropField("person", "age"), lambda s: "age" in s.tables["person"].fields),
        ],
    )
    def test_a_removal_takes_the_definition_out(self, operation: Operation, removed: Any) -> None:
        before = _person(R17, R20, R22)
        assert removed(before)
        assert not removed(operation.state_forwards(before))

    def test_each_definition_is_placed(self) -> None:
        state = _person(R17, R20, R21, R22, R14)
        assert state.analyzers["english"] == AnalyzerState(
            "english", ("class", "blank"), ("lowercase", "snowball(english)"), raw=render.read_own(R17.forwards()).raw
        )
        assert state.accesses["account"].signup_fields == {
            "email": "$email",
            "id": "$email",
            "password": "crypto::argon2::generate($password)",
        }
        assert state.functions["fn::greet"].parameters == ("n",)
        assert state.tables["post"].indexes["post_body"].search_analyzer == "english"
        assert state.tables["post"].schema_mode == "SCHEMALESS"  # the implicit table

    def test_raw_sql_applies_its_state_operations_in_order(self) -> None:
        raw = RawSQL(
            "DEFINE TABLE t; DEFINE FIELD a ON t TYPE int;",
            state_operations=[CreateTable(name="t"), AddField("t", "a", "int"), AlterField("t", "a", "string")],
        )
        table = raw.state_forwards(SchemaState()).tables["t"]
        assert table.schema_mode == "SCHEMAFULL"
        assert table.fields["a"].field_type == "string"
        assert RawSQL("UPDATE a SET b = 1;").state_forwards(_person()) == _person()

    @pytest.mark.parametrize("operation", [R24, DataMigration(forwards_sql="UPDATE a SET b = 1;")])
    def test_rebuild_index_and_data_migration_leave_the_state(self, operation: Operation) -> None:
        assert operation.state_forwards(_person()) == _person()

    def test_the_other_parts_of_the_state_are_kept(self) -> None:
        start = SchemaState(connection="other", issues=[SchemaIssue("table", "x", None, "unreadable")])
        state = AddField("t", "a", "int").state_forwards(start)
        assert (state.connection, state.issues) == ("other", start.issues)

    def test_every_operation_on_a_small_state(self) -> None:
        state = _replay(R1, R6, R9, R10, R13, R14, R15, R17, R18, R20, R22, R28, R3, R4, R30)
        assert sorted(state.tables) == ["cf", "doc", "likes", "person", "person_stats", "post", "t"]
        assert state.tables["person_stats"].view_query == "SELECT count() AS n FROM person GROUP ALL"
        assert state.tables["likes"].relation_out == ("person", "page")
        assert (state.tables["cf"].drop, state.tables["cf"].changefeed_include_original) == (True, True)
        assert state.tables["t"].fields["d"].default_always is True
        assert state.tables["person"].fields["joined"].permissions == {
            "select": "FULL",
            "create": "FULL",
            "update": "NONE",
        }


#: Each reversible operation and a state holding what its ``previous``/``previous_*`` describe (and, for a creation,
#: not its definition): its ``state_backwards()`` of its ``state_forwards()`` gives that state back.
def _round_trips() -> list[tuple[Operation, SchemaState]]:
    person = _person()
    with_r17 = _person(R17)
    with_r20 = _person(R20)
    with_r22 = _person(R22)
    plain_index = _person(DropIndex("person", "person_email"), CreateIndex("person", "person_email", ["email"]))
    return [
        (R1, SchemaState()),
        (R2, person),
        (R5, person),
        (R33, _replay(CreateTable(name="t", schema_mode="SCHEMALESS", table_type="normal"), AddField("t", "bag", "any"))),
        (R6, _person(DropField("person", "age"))),
        (R9, person),
        (R11, person),
        (R29, _replay(AddField("t", "c", "int", computed="a + 2"))),
        (R16, plain_index),
        (R13, _person(DropIndex("person", "person_email"))),
        (R17, person),
        (DefineAnalyzer("english", ["blank"], previous=R17), with_r17),
        (RemoveAnalyzer("english", previous=R17), with_r17),
        (DefineEvent("audit", "person", None, "CREATE b", previous=R18), person),
        (RemoveEvent("audit", "person", previous=R18), person),
        (R18, _person(RemoveEvent("audit", "person"))),
        (R20, person),
        (DefineAccess("account", "users", {"email": "$email"}, "email = $email", previous=R20), with_r20),
        (RemoveAccess("account", previous=R20), with_r20),
        (R22, person),
        (DefineFunction("greet", "RETURN 1;", previous=R22), with_r22),
        (RemoveFunction("greet", previous=R22), with_r22),
        (DropIndex("person", "person_email", previous=R13), person),
        (R24, person),
        (DataMigration(forwards_sql="UPDATE a SET b = 1;", backwards_sql="UPDATE a SET b = 0;"), person),
        (
            RawSQL(
                "DEFINE FIELD nick ON person TYPE string; DEFINE FIELD OVERWRITE age ON person TYPE float;",
                reverse_sql="REMOVE FIELD nick ON person; DEFINE FIELD OVERWRITE age ON person TYPE int DEFAULT 18;",
                state_operations=[
                    AddField("person", "nick", "string"),
                    AlterField("person", "age", "float", previous_type="int", previous_default=18),
                ],
            ),
            person,
        ),
    ]


class TestStateBackwards:
    """Spec §6.2 (C8, D39): the state after ``backwards()``, from the state with the operation applied."""

    def test_alter_table_places_its_previous_clauses_and_keeps_the_fields(self) -> None:
        after = R5.state_forwards(_person())
        table = R5.state_backwards(after).tables["person"]
        assert (table.schema_mode, table.comment) == ("SCHEMAFULL", "People")
        assert table.fields == _person().tables["person"].fields

    def test_alter_field_places_its_previous_field(self) -> None:
        after = R11.state_forwards(_person())
        assert after.tables["person"].fields["age"].default == "21"
        assert R11.state_backwards(after).tables["person"].fields["age"].default == "18"

    def test_alter_field_on_a_missing_table_places_the_implicit_table_first(self) -> None:
        state = R11.state_backwards(SchemaState())
        assert (state.tables["person"].schema_mode, state.tables["person"].table_type) == ("SCHEMALESS", "any")
        assert state.tables["person"].fields["age"].default == "18"

    @pytest.mark.parametrize(
        "operation",
        [
            R16,
            DropIndex("person", "person_email", previous=R13),
            DefineAnalyzer("english", ["blank"], previous=R17),
            RemoveAnalyzer("english", previous=R17),
            DefineEvent("audit", "person", None, "CREATE b", previous=R18),
            RemoveEvent("audit", "person", previous=R18),
            DefineAccess("account", "users", {"email": "$email"}, "email = $email", previous=R20),
            RemoveAccess("account", previous=R20),
            DefineFunction("greet", "RETURN 1;", previous=R22),
            RemoveFunction("greet", previous=R22),
        ],
    )
    def test_each_previous_is_placed(self, operation: Any) -> None:
        # The definition previous= describes, read as its own state_forwards() reads it (the implicit table included).
        expected = operation.previous.state_forwards(SchemaState())
        assert operation.state_backwards(SchemaState()) == expected
        assert operation.state_backwards(operation.state_forwards(SchemaState())) == expected

    @pytest.mark.parametrize(
        ("operation", "present"),
        [
            (R2, lambda s: "log" in s.tables),
            (R6, lambda s: "age" in s.tables["person"].fields),
            (R13, lambda s: "person_email" in s.tables["person"].indexes),
            (R17, lambda s: "english" in s.analyzers),
            (R18, lambda s: "audit" in s.tables["person"].events),
            (R20, lambda s: "account" in s.accesses),
            (R22, lambda s: "fn::greet" in s.functions),
        ],
    )
    def test_a_creation_is_taken_out(self, operation: Operation, present: Any) -> None:
        after = operation.state_forwards(_person(R17, R20, R22, CreateTable(name="log")))
        assert present(after)
        assert not present(operation.state_backwards(after))

    @pytest.mark.parametrize("operation", [R2, R6, R13, R17, R18, R20, R22, AddField("nope", "a", "int")])
    def test_a_creation_missing_from_the_state_leaves_it(self, operation: Operation) -> None:
        empty = SchemaState()
        assert operation.state_backwards(empty) == empty

    def test_raw_sql_undoes_its_state_operations_in_reverse_order(self) -> None:
        raw = RawSQL(
            "DEFINE FIELD nick ON person TYPE string; DEFINE FIELD OVERWRITE nick ON person TYPE int;",
            reverse_sql="REMOVE FIELD nick ON person;",
            state_operations=[
                AddField("person", "nick", "string"),
                AlterField("person", "nick", "int", previous_type="string"),
            ],
        )
        before = _person()
        after = raw.state_forwards(before)
        assert after.tables["person"].fields["nick"].field_type == "int"
        assert raw.state_backwards(after) == before

    @pytest.mark.parametrize(
        "operation",
        [
            R24,
            DataMigration(forwards_sql="UPDATE a SET b = 1;"),
            DataMigration(forwards_sql="UPDATE a SET b = 1;", backwards_sql="UPDATE a SET b = 0;"),
        ],
    )
    def test_rebuild_index_and_data_migration_leave_the_state(self, operation: Operation) -> None:
        assert operation.state_backwards(_person()) == _person()

    @pytest.mark.parametrize(
        "operation",
        [
            DropTable("person"),
            DropField("person", "age"),
            DropIndex("person", "person_email"),
            RemoveAnalyzer("english"),
            RemoveEvent("audit", "person"),
            RemoveAccess("account"),
            RemoveFunction("greet"),
            CreateIndex("person", "person_email", ["email"], overwrite=True),
            DefineEvent("audit", "person", None, "CREATE a", overwrite=True),
            DefineAccess("account", "users", {"email": "$email"}, "email = $email", overwrite=True),
            DefineFunction("greet", "RETURN 1;", overwrite=True),
            AlterTable(name="person", schema_mode="SCHEMAFULL"),
            AlterField("person", "age", "float"),
            RawSQL("UPDATE a SET b = 1;"),
            RawSQL("REMOVE TABLE log;", reverse_sql="DEFINE TABLE log;", state_operations=[DropTable("log")]),
        ],
    )
    def test_an_irreversible_operation_raises(self, operation: Operation) -> None:
        with pytest.raises(ValueError) as raised:
            operation.state_backwards(_person())
        assert str(raised.value) == f"{operation.describe()} is not reversible."

    def test_raw_sql_reversible_per_d39(self) -> None:
        dropping = RawSQL("REMOVE TABLE t;", reverse_sql="DEFINE TABLE t;", state_operations=[DropTable("t")])
        adding = RawSQL(
            "DEFINE FIELD a ON t;", reverse_sql="REMOVE FIELD a ON t;", state_operations=[AddField("t", "a", "any")]
        )
        assert (dropping.reversible, adding.reversible) == (False, True)

    @pytest.mark.parametrize(("operation", "state"), _round_trips())
    def test_the_round_trip(self, operation: Operation, state: SchemaState) -> None:
        assert operation.reversible
        # Task 4 states this as ``state.diff(...) == []``; until the diff exists, the states compare equal outright
        # (the replay reads every definition through read_own(), so even ``raw`` matches).
        assert operation.state_backwards(operation.state_forwards(state)) == state


def test_split_destructive() -> None:
    operations: list[Operation] = [
        R2,
        DropTable("old"),
        R6,
        RemoveEvent("audit", "person", previous=R18),
        DropIndex("person", "person_email", previous=R13),
        R11,
        RemoveAnalyzer("english"),
        RawSQL("UPDATE a SET b = 1;"),
        AlterTable(name="t", schema_mode="SCHEMAFULL"),
    ]
    keep, destructive = split_destructive(operations)
    assert keep == [operations[0], operations[2], operations[5], operations[7], operations[8]]
    assert destructive == [operations[1], operations[3], operations[4], operations[6]]
    assert split_destructive([]) == ([], [])


class TestFromState:
    """Spec §5.3's class methods: every attribute the state holds that the operation can write."""

    def test_a_table(self) -> None:
        state = TableState(
            "cf",
            schema_mode="SCHEMALESS",
            table_type="normal",
            changefeed="1h",
            changefeed_include_original=True,
            drop=True,
            comment="c",
            permissions={"select": "FULL", "create": "NONE", "update": "NONE", "delete": "NONE"},
        )
        created = CreateTable.from_table_state(state)
        assert (created.changefeed, created.drop, created.comment) == ("1h INCLUDE ORIGINAL", True, "c")
        assert created.permissions == {"select": "FULL", "create": "NONE", "update": "NONE", "delete": "NONE"}
        assert created.forwards() == (
            "DEFINE TABLE `cf` TYPE NORMAL DROP SCHEMALESS COMMENT 'c' CHANGEFEED 1h INCLUDE ORIGINAL PERMISSIONS FOR "
            "select FULL, FOR create, update, delete NONE;"
        )
        altered = AlterTable.from_table_state(state)
        assert (altered.changefeed, altered.drop, altered.reversible) == ("1h INCLUDE ORIGINAL", True, False)

    def test_default_table_permissions_become_none(self) -> None:
        assert CreateTable.from_table_state(TableState("t")).permissions is None
        none = {"select": "NONE", "create": "NONE", "update": "NONE", "delete": "NONE"}
        assert CreateTable.from_table_state(TableState("t", permissions=none)).permissions is None

    def test_a_view_and_a_relation(self) -> None:
        view = render.read_own(view_statement("v", "SELECT * FROM person", True, TableClauses(comment="vw")))
        assert isinstance(view, TableState)
        created = CreateTable.from_table_state(view)
        assert (created.table_type, created.view_query) == (None, "SELECT * FROM person")
        relation = render.read_own(R4.forwards())
        assert isinstance(relation, TableState)
        assert CreateTable.from_table_state(relation) == R4

    def test_a_table_round_trips(self) -> None:
        for operation in (R1, R4, R30):  # R2 writes no TYPE clause; read back TYPE ANY, it is written TYPE ANY
            state = render.read_own(operation.forwards())
            assert isinstance(state, TableState)
            assert CreateTable.from_table_state(state).forwards() == operation.forwards()

    def test_alter_table_from_two_states(self) -> None:
        current = TableState("person", schema_mode="SCHEMAFULL", comment="People", drop=True)
        target = TableState("person", schema_mode="SCHEMALESS", table_type="any", changefeed="7d")
        altered = AlterTable.from_table_states(current, target)
        assert (altered.schema_mode, altered.table_type, altered.changefeed, altered.drop) == ("SCHEMALESS", "any", "7d", False)
        assert (altered.previous_schema_mode, altered.previous_comment, altered.previous_drop) == ("SCHEMAFULL", "People", True)
        assert altered.reversible
        assert altered.forwards() == "DEFINE TABLE OVERWRITE `person` TYPE ANY SCHEMALESS CHANGEFEED 7d;"
        assert altered.backwards() == "DEFINE TABLE OVERWRITE `person` TYPE NORMAL DROP SCHEMAFULL COMMENT 'People';"

    def test_a_field(self) -> None:
        always = FieldState("d", "int", False, "7", default_always=True, comment="c", readonly=True, assertion="$value > 0")
        field = AddField.from_field_state("t", always)
        assert (field.default, field.default_always, field.comment, field.readonly, field.assertion) == (
            7,
            True,
            "c",
            True,
            "$value > 0",
        )
        computed = AddField.from_field_state("t", FieldState("c", "int", False, computed="a + 1", flexible=True))
        assert (computed.computed, computed.default, computed.flexible) == ("a + 1", None, True)
        nullable = AddField.from_field_state("t", FieldState("n", "string", True, reference=False))
        assert nullable.forwards() == "DEFINE FIELD `n` ON `t` TYPE option<string>;"
        reference = AddField.from_field_state("t", FieldState("o", "record<user>", True, reference=True, on_delete="CASCADE"))
        assert (reference.reference, reference.on_delete) == (True, "CASCADE")
        dotted = AddField.from_field_state("t", FieldState("a.b", "int", False, path=("a.b",)))
        assert dotted.path == ("a.b",)
        assert AddField.from_field_state("t", FieldState("a.b", "int", False)).path == ("a", "b")

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("18", 18),
            ("-3", -3),
            ("true", True),
            ("'a;b'", "a;b"),
            ("1.5f", 1.5),
            ("1.5", 1.5),
            ("1.1dec", Decimal("1.1")),
            ("d'2026-01-01T00:00:00Z'", datetime(2026, 1, 1, tzinfo=UTC)),
            ("time::now()", DefaultExpression("time::now()")),
            ("NONE", DefaultExpression("NONE")),
            ("NULL", DefaultExpression("NULL")),
            ("[]", DefaultExpression("[]")),
            ("{ LET $x = 1; RETURN $x; }", DefaultExpression("{ LET $x = 1; RETURN $x; }")),
        ],
    )
    def test_a_default_text(self, text: str, expected: object) -> None:
        default = AddField.from_field_state("t", FieldState("a", "any", False, text)).default
        assert default == expected
        assert type(default) is type(expected)

    def test_default_field_permissions_become_none(self) -> None:
        full = {"select": "FULL", "create": "FULL", "update": "FULL"}
        assert AddField.from_field_state("t", FieldState("a", "int", permissions=full)).permissions is None
        assert AddField.from_field_state("t", FieldState("a", "int")).permissions is None
        rules = {"select": "FULL", "create": "FULL", "update": "NONE"}
        assert AddField.from_field_state("t", FieldState("a", "int", permissions=rules)).permissions == rules

    def test_a_field_round_trips(self) -> None:
        for operation in (R6, R7, R8, R9, R10, R28, AddField("t", "c", "int", computed="a + 1")):
            state = render.read_own(operation.forwards())
            assert isinstance(state, FieldState)
            assert AddField.from_field_state(operation.table, state).forwards() == operation.forwards()

    def test_alter_field_from_two_states(self) -> None:
        current = FieldState("age", "int", False, "18", assertion="$value > 0", default_always=True)
        target = FieldState("age", "float", True, "21.5f", comment="years")
        altered = AlterField.from_field_states("person", current, target)
        assert (altered.field_type, altered.nullable, altered.default, altered.comment) == ("float", True, 21.5, "years")
        assert (altered.previous_type, altered.previous_default, altered.previous_assertion) == ("int", 18, "$value > 0")
        assert altered.previous_default_always is True
        assert altered.forwards() == "DEFINE FIELD OVERWRITE `age` ON `person` TYPE option<float> DEFAULT 21.5 COMMENT 'years';"
        assert altered.backwards() == ("DEFINE FIELD OVERWRITE `age` ON `person` TYPE int DEFAULT ALWAYS 18 ASSERT $value > 0;")
        computed = AlterField.from_field_states(
            "t", FieldState("c", "int", False, computed="a + 2"), FieldState("c", "int", False, computed="a + 1")
        )
        assert computed == R29

    def test_an_index(self) -> None:
        assert CreateIndex.from_index_state("t", IndexState("m", ("e",), mtree=True)) is None
        assert CreateIndex.from_index_state("t", IndexState("c", (), count=True)) is None
        assert CreateIndex.from_index_state("t", IndexState("x", ("tags.*",))) is None
        assert CreateIndex.from_index_state("t", IndexState("x", ("a",), concurrently=True)) is None
        for operation in (R13, R14, CreateIndex("t", "ft", ["x"], search_analyzer="en", bm25=(1.5, 0.5), comment="c")):
            state = render.read_own(operation.forwards())
            assert isinstance(state, IndexState)
            assert CreateIndex.from_index_state(operation.table, state) == operation
        hnsw = render.read_own(R15.forwards())  # every HNSW option is written, so read back as declared
        assert isinstance(hnsw, IndexState)
        assert CreateIndex.from_index_state("doc", hnsw) == dataclasses.replace(R15, vector_type="F32", efc=150, m=12)

    def test_an_index_with_the_server_s_bm25_defaults(self) -> None:
        state = IndexState("ft", ("body",), search_analyzer="english", bm25=(1.2, 0.75), table="post")
        assert CreateIndex.from_index_state("post", state) == CreateIndex(
            "post", "ft", ["body"], search_analyzer="english", bm25=(1.2, 0.75)
        )

    def test_an_analyzer(self) -> None:
        state = AnalyzerState("odd", ("blank",), ("edgengram(10,2)",))  # M32: stored by SurrealDB, refused by the lite
        assert DefineAnalyzer.from_analyzer_state(state) is None
        fixed = DefineAnalyzer.from_analyzer_state(AnalyzerState("odd", ("blank",), ("edgengram(2,10)",)))
        assert fixed is not None
        assert fixed.forwards() == "DEFINE ANALYZER OVERWRITE `odd` TOKENIZERS blank FILTERS edgengram(2,10);"
        read = render.read_own(R17.forwards())
        assert isinstance(read, AnalyzerState)
        assert DefineAnalyzer.from_analyzer_state(read) == R17

    def test_an_event(self) -> None:
        for operation in (R18, R19, DefineEvent("e", "t", None, "CREATE a; CREATE b", comment="c")):
            state = render.read_own(operation.forwards())
            assert isinstance(state, EventState)
            rebuilt = DefineEvent.from_event_state(state)
            assert rebuilt is not None
            assert render.read_own(rebuilt.forwards()) == state  # the block is not wrapped twice
        multi_line = EventState("e", "t", "$event = 'CREATE'", "{\n\tCREATE a;\n\tCREATE b;\n}")
        rebuilt = DefineEvent.from_event_state(multi_line)
        assert rebuilt is not None
        assert rebuilt.then == "CREATE a;\n\tCREATE b;"
        assert DefineEvent.from_event_state(EventState("e", "t", "a; b", "CREATE a")) is None

    def test_an_access(self) -> None:
        assert DefineAccess.from_access_state(AccessState("api", access_type="JWT")) is None
        server = (
            "DEFINE ACCESS account ON DATABASE TYPE RECORD SIGNUP (CREATE users SET id = $email, email = $email, "
            "password = crypto::argon2::generate($password)) SIGNIN (SELECT * FROM users WHERE email = $email AND "
            "crypto::argon2::compare(password, $password)) WITH JWT ALGORITHM HS512 KEY '[REDACTED]' WITH ISSUER KEY "
            "'[REDACTED]' DURATION FOR TOKEN 15m, FOR SESSION 12h"
        )
        access = DefineAccess.from_access_state(parse_define_access(server))
        assert access == R21
        assert access is not None
        assert list(access.signup_fields) == ["email", "id", "password"]  # the parser's order; id is written first
        assert access.forwards() == R21.forwards()
        authenticate = server.replace(" DURATION", " AUTHENTICATE { RETURN $auth } DURATION")
        assert DefineAccess.from_access_state(dataclasses.replace(parse_define_access(server), raw=authenticate)) is None
        rs = server.replace("HS512 KEY '[REDACTED]'", "RS256 KEY '[REDACTED]'")
        assert DefineAccess.from_access_state(dataclasses.replace(parse_define_access(server), raw=rs)) is None
        url = server.replace("ALGORITHM HS512 KEY '[REDACTED]'", "URL 'https://example.com/jwks'")
        assert DefineAccess.from_access_state(dataclasses.replace(parse_define_access(server), raw=url)) is None
        other = dataclasses.replace(parse_define_access(server), table="", signup_fields={})
        assert DefineAccess.from_access_state(other) is None
        assert DefineAccess.from_access_state(dataclasses.replace(parse_define_access(server), signin_where="")) is None
        assert DefineAccess.from_access_state(dataclasses.replace(parse_define_access(server), duration_token=None)) is None

    def test_an_access_with_refresh(self) -> None:
        refresh = DefineAccess(
            "a", "users", {"email": "$email"}, "email = $email", with_refresh=True, duration_grant="7d", comment="c"
        )
        state = render.read_own(refresh.forwards())
        assert isinstance(state, AccessState)
        assert DefineAccess.from_access_state(state) == refresh


class TestDefaultExpression:
    @pytest.mark.parametrize("text", ["'a;b'", "{ LET $x = 1; RETURN $x; }", "time::now()", "NONE", " [1, 2] "])
    def test_one_expression_is_accepted(self, text: str) -> None:
        assert DefaultExpression(text).text == text.strip()

    @pytest.mark.parametrize(
        ("text", "reason"),
        [
            ("1; 2", "it holds a ';' outside brackets"),
            ("1 -- 2", "comment marker"),
            ("'a", "string literal is not terminated"),
            ("(1", "brackets"),
            ("", "it is empty"),
            ("a\x00b", "NUL"),
        ],
    )
    def test_a_refusal_never_holds_the_text(self, text: str, reason: str) -> None:
        with pytest.raises(ValueError, match=re.escape(reason)) as raised:
            DefaultExpression(text)
        assert str(raised.value).startswith("DefaultExpression: the text is not one SurrealQL expression: ")
        if text.strip():
            assert text not in str(raised.value)

    def test_a_non_str(self) -> None:
        with pytest.raises(TypeError, match="DefaultExpression: the text must be a str, got int"):
            DefaultExpression(1)  # type: ignore[arg-type]

    def test_repr_hides_the_text(self) -> None:
        assert repr(DefaultExpression("'s3cr3t-marker'")) == "DefaultExpression(...)"
        assert "s3cr3t-marker" not in str(DefaultExpression("'s3cr3t-marker'"))

    def test_equality_frozen_and_pickle(self) -> None:
        expression = DefaultExpression("time::now()")
        assert expression == DefaultExpression(" time::now() ")
        assert expression != DefaultExpression("time::unix()")
        with pytest.raises(dataclasses.FrozenInstanceError):
            expression.text = "x"  # type: ignore[misc]
        assert pickle.loads(pickle.dumps(expression)) == expression


class TestOperationRepr:
    _SECRET = "s3cr3t-marker"

    @pytest.mark.parametrize(
        "operation",
        [
            RawSQL(f"UPDATE a SET b = '{_SECRET}';", reverse_sql=f"UPDATE a SET b = '{_SECRET}2';"),
            AddField("t", "a", "string", value=f"'{_SECRET}'"),
            AddField("t", "a", "string", default=DefaultExpression(f"'{_SECRET}'")),
            AddField("t", "a", "string", default=_SECRET, assertion=f"$value != '{_SECRET}'"),
            CreateTable(name="t", permissions={"select": f"WHERE a = '{_SECRET}'"}),
            AlterField("t", "a", "string", previous_type="string", previous_value=f"'{_SECRET}'"),
            DefineEvent("e", "t", f"$value.a = '{_SECRET}'", f"CREATE log SET s = '{_SECRET}'"),
            DefineAccess("a", "u", {"k": f"'{_SECRET}'"}, f"k = '{_SECRET}'"),
            DefineFunction("f", f"RETURN '{_SECRET}';", permissions=f"WHERE $auth.k = '{_SECRET}'"),
            DataMigration(forwards_sql=f"UPDATE a SET b = '{_SECRET}';", backwards_sql=f"UPDATE a SET b = '{_SECRET}';"),
            CreateTable(name="v", view_query=f"SELECT * FROM a WHERE b = '{_SECRET}'"),
            RemoveEvent("e", "t", previous=DefineEvent("e", "t", None, f"CREATE log SET s = '{_SECRET}'")),
        ],
    )
    def test_repr_holds_no_surrealql_text(self, operation: Operation) -> None:
        assert self._SECRET not in repr(operation)
        assert self._SECRET not in str(operation)
        assert type(operation).__name__ in repr(operation)

    def test_repr_shows_names_kinds_and_flags(self) -> None:
        text = repr(R9)
        assert "table='person'" in text
        assert "name='joined'" in text
        assert "field_type='datetime'" in text
        assert "readonly=True" in text
        assert "comment='Join date'" in text


class TestPickle:
    @pytest.mark.parametrize(
        "operation",
        [
            AlterField(
                "person",
                "age",
                "float",
                default=DefaultExpression("1.5 + 1"),
                previous_type="int",
                previous_default=18,
                previous_assertion="$value > 0",
                previous_permissions={"update": "NONE"},
                previous_default_always=True,
            ),
            RemoveEvent("audit", "person", previous=R18),
            R1,
            R9,
            R16,
            R20,
            R22,
            RawSQL("UPDATE a SET b = 1;", reverse_sql="UPDATE a SET b = 0;", state_operations=[R2, R6]),
            DataMigration(forwards_sql="UPDATE a SET b = 1;", forwards_func=_noop),
        ],
    )
    @pytest.mark.parametrize("protocol", [2, pickle.HIGHEST_PROTOCOL])
    def test_round_trip(self, operation: Operation, protocol: int) -> None:
        clone = pickle.loads(pickle.dumps(operation, protocol=protocol))
        assert clone == operation
        assert type(clone) is type(operation)
        assert clone.forwards() == operation.forwards()
        assert clone.backwards() == operation.backwards()


class TestEdges:
    """The remaining refusals and readings of the operations' checkers."""

    @pytest.mark.parametrize(
        ("build", "error", "message"),
        [
            (lambda: AddField("t", "a", "int", comment="a\x00b"), ValueError, "comment= holds a NUL"),
            (
                lambda: AlterField("t", "a", previous_type="int", previous_comment=1),
                TypeError,
                "previous_comment= must be a str",
            ),
            (lambda: AddField("t", "a", "int", path=1), TypeError, "path= must be a list or tuple of str, got int"),
            (lambda: AddField("t", "a", "int", path=(1,)), TypeError, "path= holds a int, not a str"),
            (lambda: AddField("t", "a", None), TypeError, "field_type= must be a str"),
            (lambda: AddField("t", "", "int"), ValueError, "name= must be a non-empty str"),
            (
                lambda: CreateIndex("t", "i", ["a"], search_analyzer=Analyzer("en", ["blank"])),
                TypeError,
                "search_analyzer= names an analyzer (a str), got Analyzer",
            ),
            (lambda: DefineAccess("a", "u", {1: "$e"}, "e = $e"), TypeError, "signup_fields= holds a int column"),
            (lambda: DefineAccess("a", "u", {"e": None}, "e = $e"), TypeError, "signup_fields['e'] must be a str"),
            (lambda: DefineAccess("a", "u", {"e": "$e"}, None), TypeError, "signin_where= must be a str"),
            (lambda: RawSQL("UPDATE a SET b = 1;", reverse_sql=None), TypeError, "reverse_sql= must be a str, got NoneType"),
            (lambda: RawSQL(1), TypeError, "RawSQL: the SQL must be a str, got int"),
            (lambda: RawSQL("UPDATE a SET b = 1;", description=None), TypeError, "description= must be a str"),
            (lambda: DataMigration(forwards_sql=" "), ValueError, "DataMigration: forwards_sql= holds no statement"),
            (
                lambda: DataMigration(forwards_sql="UPDATE a SET b = 1;", backwards_sql=" ; "),
                ValueError,
                "backwards_sql= holds no statement",
            ),
            (lambda: DefineEvent("e", "t", None, "CREATE a", overwrite=1), TypeError, "overwrite= must be a bool"),
            (lambda: CreateIndex("t", "i", ["a"], concurrently=1), TypeError, "concurrently= must be a bool"),
            (lambda: DefineFunction("f", "RETURN 1;", overwrite="no"), TypeError, "overwrite= must be a bool"),
            (lambda: DefineFunction(1, "RETURN 1;"), TypeError, "DefineFunction: Function name must be a string"),
            (lambda: AlterTable(name="t", previous_schema_mode="loose"), ValueError, "previous_schema_mode= must be"),
            (lambda: AlterTable(name="t", previous_changefeed="x"), ValueError, "previous_changefeed= is 'x'"),
        ],
    )
    def test_refusals(self, build: Any, error: type[Exception], message: str) -> None:
        with pytest.raises(error, match=re.escape(message)):
            build()

    def test_a_backwards_func_alone_makes_a_data_migration_reversible(self) -> None:
        data = DataMigration(forwards_sql="UPDATE a SET b = 1;", backwards_sql="", backwards_func=_noop)
        assert (data.reversible, data.backwards()) == (True, "")

    def test_alter_table_without_a_clause(self) -> None:
        assert AlterTable(name="t").forwards() == "DEFINE TABLE OVERWRITE `t`;"
        assert render.read_own(AlterTable(name="t").forwards()) == TableState(
            "t", schema_mode="SCHEMALESS", table_type="any", raw="DEFINE TABLE `t`"
        )

    def test_a_view_with_every_clause(self) -> None:
        view = CreateTable(
            name="v",
            schema_mode="SCHEMAFULL",
            view_query="SELECT * FROM person;",
            comment="vw",
            changefeed="1d",
            permissions={"select": "FULL"},
            drop=True,
        )
        assert view.view_query == "SELECT * FROM person"
        assert view.forwards() == (
            "DEFINE TABLE `v` DROP SCHEMAFULL AS (SELECT * FROM person) COMMENT 'vw' CHANGEFEED 1d PERMISSIONS FOR select "
            "FULL, FOR create, update, delete NONE;"
        )
        state = view.state_forwards(SchemaState()).tables["v"]
        assert (state.view_query, state.comment, state.drop) == ("SELECT * FROM person", "vw", True)

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("{ CREATE a }", "CREATE a"),
            ("{ { CREATE a } }", "{ CREATE a }"),
            ("{ CREATE a } + { CREATE b }", "{ CREATE a } + { CREATE b }"),
            ("{ CREATE a; '}' }", "CREATE a; '}'"),
            ("(CREATE a)", "(CREATE a)"),
            ("{ CREATE a", "{ CREATE a"),
        ],
    )
    def test_an_event_s_block_is_unwrapped_once(self, text: str, expected: str) -> None:
        assert _block_content(text) == expected

    def test_an_access_the_constructor_refuses_reads_as_none(self) -> None:
        state = AccessState("a", "users", {"e mail": "$e"}, "e = $e")
        assert DefineAccess.from_access_state(state) is None
        broken = AccessState("a", "users", {"email": "$email"}, "email = $email", raw="DEFINE ACCESS a SIGNUP (CREATE 'x")
        assert DefineAccess.from_access_state(broken) is None

    def test_an_index_the_constructor_refuses_reads_as_none(self) -> None:
        assert CreateIndex.from_index_state("user-events", IndexState("i", ("a",))) is None
        assert CreateIndex.from_index_state("t", IndexState("i", ("a",), bm25=True)) is None  # BM25 without an analyzer


class TestPlaceAndTakeOut:
    """``state.place()`` and ``state.take_out()`` (spec §6.2), the helpers ``state_forwards()``/``state_backwards()`` use."""

    def test_place_keeps_what_a_table_holds(self) -> None:
        before = _person()
        after = mstate.place(before, TableState("person", schema_mode="SCHEMALESS", other={"lives": {"x": "y"}}))
        assert after.tables["person"].fields == before.tables["person"].fields
        assert after.tables["person"].other == before.tables["person"].other
        assert after.tables["person"].schema_mode == "SCHEMALESS"

    def test_place_a_definition_without_its_table(self) -> None:
        with pytest.raises(ValueError, match="place\\(\\): the FieldState 'a' names no table"):
            mstate.place(SchemaState(), FieldState("a", "int"))

    def test_take_out_needs_the_table_of_a_field(self) -> None:
        with pytest.raises(ValueError, match="take_out\\(\\): a field needs table="):
            mstate.take_out(_person(), "field", "age")

    def test_take_out_an_unknown_kind(self) -> None:
        with pytest.raises(ValueError, match="take_out\\(\\): unknown kind 'param'"):
            mstate.take_out(_person(), "param", "x")  # type: ignore[arg-type]

    def test_take_out_leaves_the_state_when_missing(self) -> None:
        before = _person()
        for kind in ("field", "index", "event"):
            assert mstate.take_out(before, kind, "nope", "person") is before  # type: ignore[arg-type]
        for kind in ("table", "analyzer", "access", "function"):
            assert mstate.take_out(before, kind, "nope") is before  # type: ignore[arg-type]
