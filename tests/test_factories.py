"""``surreal_orm_lite.testing`` model factories (v0.40.0): ``Faker`` and ``ModelFactory``, mirroring the full ORM.

``Faker`` generates test data with the full SurrealDB-ORM's fourteen providers, pools and output formats; the lite
checks a provider and its keywords when the ``Faker`` is built rather than when it generates. ``ModelFactory``
collects its declarations when the class is created and checks them against ``Meta.model`` there. The classes that
open a server or the embedded engine are suffixed ``E2E``.
"""

import ast
import contextlib
import enum
import functools
import random
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field, ValidationError
from surrealdb import RecordID

import surreal_orm_lite.testing.factories as factories_module
from surreal_orm_lite import (
    BaseSurrealEdge,
    BaseSurrealModel,
    SurrealConfigDict,
    SurrealDBConnectionManager,
    SurrealDbError,
    SurrealFunc,
    post_save,
    pre_save,
)
from surreal_orm_lite.testing import SurrealFixture, embedded_database, fixture
from surreal_orm_lite.testing.factories import Faker, ModelFactory
from tests._retry import apply_reset
from tests.conftest import _ws_url, orm_client
from tests.test_embedded import _embedded_engine_available


class FxUser(BaseSurrealModel):
    """A plain node model for the factory tests."""

    model_config = SurrealConfigDict(table_name="fx_user")

    id: str | None = None
    name: str = ""
    email: str = ""
    age: int = 0
    role: str = "player"


class FxSecret(BaseSurrealModel):
    """A model with an aliased field: Python name ``password``, column ``password_hash``."""

    model_config = SurrealConfigDict(table_name="fx_secret")

    id: str | None = None
    password: str | None = Field(None, alias="password_hash")


class FxKnows(BaseSurrealEdge):
    """An edge model: endpoints ``in_`` (column ``in``) and ``out``."""

    model_config = SurrealConfigDict(table_name="fx_knows")

    since: int = 0


class FxFree(BaseSurrealModel):
    """A model that accepts any extra field."""

    model_config = SurrealConfigDict(table_name="fx_free", extra="allow")

    id: str | None = None


class FxByColumn(BaseSurrealModel):
    """An aliased field on a model that validates by column only (``populate_by_name=False``): P2."""

    model_config = SurrealConfigDict(table_name="fx_bycol", populate_by_name=False)

    id: str | None = None
    pw: str | None = Field(None, alias="pw_hash")


class FxCounted(BaseSurrealModel):
    """Fields named like the classmethods' positional-only parameters ``count`` and ``cls`` (D13)."""

    model_config = SurrealConfigDict(table_name="fx_counted")

    id: str | None = None
    count: int = 0
    cls: str = ""


class FxStamped(BaseSurrealModel):
    """Optional fields a factory fills with server values; ``token`` is aliased (column ``tok``)."""

    model_config = SurrealConfigDict(table_name="fx_stamped")

    id: str | None = None
    name: str = ""
    age: int | None = None
    token: str | None = Field(None, alias="tok")
    joined: datetime | None = None


class FxTyped(BaseSurrealModel):
    """A ``date``, a ``datetime`` and a ``UUID``: the values ``create()`` must read back equal."""

    model_config = SurrealConfigDict(table_name="fx_typed")

    id: str | None = None
    born: date | None = None
    seen: datetime | None = None
    ref: uuid.UUID | None = None


class FxRequired(BaseSurrealModel):
    """A model with a required field and no default."""

    model_config = SurrealConfigDict(table_name="fx_required")

    id: str | None = None
    code: str


class FxAddress(BaseModel):
    """A sub-model, declared as an instance on a factory."""

    city: str


class FxColour(enum.Enum):
    """An enum, declared as a member on a factory."""

    RED = "red"


class _Callable:
    """An instance with ``__call__``: a callable, so refused as a factory attribute."""

    def __call__(self) -> int:
        return 1


class FxRich(BaseSurrealModel):
    """Static values a factory deep-copies: a list, a record id and a sub-model."""

    model_config = SurrealConfigDict(table_name="fx_rich")

    id: str | None = None
    tags: list[str] = Field(default_factory=list)
    owner: RecordID | None = None
    address: FxAddress | None = None


class UserFactory(ModelFactory[FxUser]):
    """The reference node factory."""

    class Meta:
        model = FxUser

    name = Faker("name")
    role = "admin"


#: The full ORM's pools (``surreal_orm/testing/factories.py``), copied as literals: the lite's must equal them.
_FULL_FIRST_NAMES = [
    "Alice",
    "Bob",
    "Charlie",
    "Diana",
    "Eve",
    "Frank",
    "Grace",
    "Hank",
    "Ivy",
    "Jack",
    "Karen",
    "Leo",
    "Mia",
    "Nathan",
    "Olivia",
    "Paul",
    "Quinn",
    "Rose",
    "Sam",
    "Tina",
    "Uma",
    "Victor",
    "Wendy",
    "Xavier",
    "Yuki",
    "Zoe",
    "Liam",
    "Emma",
    "Noah",
    "Sophia",
]
_FULL_LAST_NAMES = [
    "Smith",
    "Johnson",
    "Williams",
    "Brown",
    "Jones",
    "Garcia",
    "Miller",
    "Davis",
    "Rodriguez",
    "Martinez",
    "Hernandez",
    "Lopez",
    "Gonzalez",
    "Wilson",
    "Anderson",
    "Thomas",
    "Taylor",
    "Moore",
    "Jackson",
    "Martin",
    "Lee",
    "Perez",
    "Thompson",
    "White",
    "Harris",
    "Sanchez",
    "Clark",
    "Ramirez",
    "Lewis",
    "Robinson",
]
_FULL_DOMAINS = ["example.com", "test.org", "demo.net", "sample.io", "mock.dev"]
_FULL_WORDS = [
    "alpha",
    "bravo",
    "charlie",
    "delta",
    "echo",
    "foxtrot",
    "golf",
    "hotel",
    "india",
    "juliet",
    "kilo",
    "lima",
    "mike",
    "november",
    "oscar",
    "papa",
    "quebec",
    "romeo",
    "sierra",
    "tango",
    "uniform",
    "victor",
    "whiskey",
    "xray",
    "yankee",
    "zulu",
]

_PROVIDERS = [
    ("name", {}),
    ("first_name", {}),
    ("last_name", {}),
    ("email", {}),
    ("random_int", {}),
    ("random_float", {}),
    ("text", {}),
    ("sentence", {}),
    ("word", {}),
    ("uuid", {}),
    ("boolean", {}),
    ("date", {}),
    ("datetime", {}),
    ("choice", {"items": ["a", "b", "c"]}),
]

_EMAIL = re.compile(r"^[a-z]+\.[a-z]+\d{3}@(example\.com|test\.org|demo\.net|sample\.io|mock\.dev)$")


def _many(faker: Faker, n: int = 200) -> list[Any]:
    return [faker.generate() for _ in range(n)]


class TestFaker:
    """AC-2/3/4 (provider part): the full ORM's providers, pools and formats; checks at construction; seeding."""

    def test_fourteen_providers(self) -> None:
        assert len(_PROVIDERS) == 14
        for provider, kwargs in _PROVIDERS:
            Faker(provider, **kwargs).generate()

    def test_pools_equal_the_full_orms(self) -> None:
        from surreal_orm_lite.testing import factories

        assert factories._FIRST_NAMES == _FULL_FIRST_NAMES
        assert factories._LAST_NAMES == _FULL_LAST_NAMES
        assert factories._DOMAINS == _FULL_DOMAINS
        assert factories._WORDS == _FULL_WORDS

    def test_name(self) -> None:
        for value in _many(Faker("name")):
            first, last = value.split(" ")
            assert first in _FULL_FIRST_NAMES
            assert last in _FULL_LAST_NAMES

    def test_first_and_last_name(self) -> None:
        assert all(v in _FULL_FIRST_NAMES for v in _many(Faker("first_name")))
        assert all(v in _FULL_LAST_NAMES for v in _many(Faker("last_name")))

    def test_email(self) -> None:
        for value in _many(Faker("email")):
            assert isinstance(value, str)
            assert _EMAIL.match(value), value

    def test_random_int_defaults_and_bounds(self) -> None:
        values = _many(Faker("random_int"))
        assert all(type(v) is int and 0 <= v <= 100 for v in values)
        assert all(18 <= v <= 20 for v in _many(Faker("random_int", min=18, max=20)))
        assert set(_many(Faker("random_int", min=5, max=5), 10)) == {5}

    def test_random_int_min_greater_than_max_raises_at_generate(self) -> None:
        faker = Faker("random_int", min=10, max=1)  # construction accepts it
        with pytest.raises(ValueError):
            faker.generate()

    def test_random_float(self) -> None:
        for value in _many(Faker("random_float")):
            assert isinstance(value, float)
            assert 0.0 <= value <= 1.0
            assert round(value, 4) == value
        assert all(2.0 <= v <= 3.0 for v in _many(Faker("random_float", min=2.0, max=3.0)))

    def test_text(self) -> None:
        for value in _many(Faker("text"), 50):
            assert isinstance(value, str)
            assert len(value) <= 200
            assert all(w in _FULL_WORDS for w in value.split(" ")[:-1])
        assert all(len(v) <= 12 for v in _many(Faker("text", max_length=12)))

    def test_sentence(self) -> None:
        for value in _many(Faker("sentence")):
            assert value.endswith(".")
            assert value[0].isupper()
            words = value[:-1].lower().split(" ")
            assert 5 <= len(words) <= 12
            assert all(w in _FULL_WORDS for w in words)

    def test_word(self) -> None:
        assert all(v in _FULL_WORDS for v in _many(Faker("word")))

    def test_uuid_is_a_v4_string(self) -> None:
        for value in _many(Faker("uuid")):
            assert isinstance(value, str)
            assert uuid.UUID(value).version == 4
            assert str(uuid.UUID(value)) == value

    def test_boolean(self) -> None:
        values = _many(Faker("boolean"))
        assert all(type(v) is bool for v in values)
        assert set(values) == {True, False}

    def test_date_within_the_last_year(self) -> None:
        today = date.today()
        for value in _many(Faker("date")):
            assert type(value) is date
            assert today - timedelta(days=366) <= value <= today

    def test_datetime_aware_utc_within_the_last_year(self) -> None:
        for value in _many(Faker("datetime")):
            now = datetime.now(UTC)
            assert isinstance(value, datetime)
            assert value.tzinfo is not None
            assert value.utcoffset() == timedelta(0)
            assert now - timedelta(days=366) <= value <= now

    def test_choice(self) -> None:
        assert set(_many(Faker("choice", items=["a", "b", "c"]))) == {"a", "b", "c"}

    def test_repr(self) -> None:
        assert repr(Faker("name")) == "Faker('name')"
        assert repr(Faker("random_int", min=18, max=80)) == "Faker('random_int', min=18, max=80)"

    def test_attributes(self) -> None:
        faker = Faker("random_int", min=1, max=2)
        assert faker.provider == "random_int"
        assert faker.kwargs == {"min": 1, "max": 2}

    def test_unknown_provider(self) -> None:
        with pytest.raises(ValueError, match=re.escape("Unknown Faker provider: 'nme'")):
            Faker("nme")

    def test_private_attribute_is_not_a_provider(self) -> None:
        with pytest.raises(ValueError, match="Unknown Faker provider"):
            Faker("_gen_name")

    def test_provider_not_a_string(self) -> None:
        with pytest.raises(TypeError):
            Faker(1)  # type: ignore[arg-type]

    def test_unknown_keyword(self) -> None:
        with pytest.raises(TypeError) as info:
            Faker("random_int", minimum=1)
        assert str(info.value) == "Faker('random_int') takes no keyword 'minimum' (accepted: max, min)"

    def test_keyword_on_a_provider_without_keywords(self) -> None:
        with pytest.raises(TypeError, match="items"):
            Faker("name", items=[1])

    @pytest.mark.parametrize("kwargs", [{}, {"items": []}])
    def test_choice_requires_items(self, kwargs: dict[str, Any]) -> None:
        with pytest.raises(ValueError, match=re.escape("Faker('choice') requires 'items' kwarg")):
            Faker("choice", **kwargs)

    def test_items_copied_at_construction(self) -> None:
        items = ["a"]
        faker = Faker("choice", items=items)
        items.append("b")
        items[0] = "z"
        assert set(_many(faker, 50)) == {"a"}

    @pytest.mark.parametrize("items", [("a", "b"), ["a", "b"]])
    def test_items_stored_as_a_new_list(self, items: Any) -> None:
        faker = Faker("choice", items=items)
        assert repr(faker) == "Faker('choice', items=['a', 'b'])"
        assert type(faker.kwargs["items"]) is list
        assert faker.kwargs["items"] is not items

    def test_seeding_reproduces_every_provider(self) -> None:
        def draw() -> list[Any]:
            random.seed(7)
            return [Faker(p, **kw).generate() for p, kw in _PROVIDERS for _ in range(3)]

        first, second = draw(), draw()
        assert len(first) == len(second) == 42
        for a, b in zip(first, second, strict=True):
            if isinstance(a, datetime):
                # "now" moved between the draws; the seeded offset did not.
                assert abs(a - b) < timedelta(seconds=5)
            else:
                assert a == b


class TestFactoryDeclaration:
    """Collection, ``Meta`` checks, model resolution and name normalization at class creation (AC-5/6/21)."""

    def test_collected_values(self) -> None:
        faker = Faker("name")
        when = datetime(2026, 1, 1, tzinfo=UTC)
        rid = RecordID("fx_user", "a")
        address = FxAddress(city="Paris")
        func = SurrealFunc("time::now()")

        class Free(ModelFactory):
            a = faker
            b = "text"
            c = 3
            d = [1, 2]
            e = when
            f = rid
            g = FxColour.RED
            h = address
            i = func

        assert Free._field_defs == {
            "a": faker,
            "b": "text",
            "c": 3,
            "d": [1, 2],
            "e": when,
            "f": rid,
            "g": FxColour.RED,
            "h": address,
            "i": func,
        }

    def test_ignored_attributes(self) -> None:
        class Free(ModelFactory):
            _private = 1
            kept = 1

            class Meta:
                pass

            def method(self) -> int:
                return 1

            fn = lambda: 1  # noqa: E731

            @classmethod
            def cm(cls) -> int:
                return 1

            @staticmethod
            def sm() -> int:
                return 1

            @property
            def prop(self) -> int:
                return 1

            class Nested:
                pass

        assert Free._field_defs == {"kept": 1}

    @pytest.mark.parametrize(
        ("value", "shown"),
        [
            (datetime.now, "datetime.now"),
            (functools.partial(int, "1"), "functools.partial"),
            (_Callable(), "_Callable"),
        ],
    )
    def test_callable_refused(self, value: Any, shown: str) -> None:
        with pytest.raises(TypeError) as info:
            type("Bad", (ModelFactory,), {"joined": value})
        message = str(info.value)
        assert message.startswith("Bad.joined is a callable (")
        assert shown in message
        assert "lazy values are not supported yet" in message

    def test_callable_message_text(self) -> None:
        with pytest.raises(TypeError) as info:

            class UserFactory(ModelFactory):
                joined = datetime.now

        assert str(info.value) == (
            "UserFactory.joined is a callable (datetime.now); a factory attribute is a static value, a Faker or a "
            "SurrealFunc — lazy values are not supported yet"
        )

    def test_inheritance_override_and_order(self) -> None:
        class Parent(ModelFactory):
            a = 1
            b = 2

        class Child(Parent):
            c = 3
            a = 10

        assert Child._field_defs == {"a": 10, "b": 2, "c": 3}
        assert list(Child._field_defs) == ["a", "b", "c"]
        assert Parent._field_defs == {"a": 1, "b": 2}

    def test_model_factory_itself_has_no_declarations(self) -> None:
        assert ModelFactory._field_defs == {}

    def test_subscription_alone_runs_no_check(self) -> None:
        alias = ModelFactory[FxUser]
        assert alias is not None
        assert ModelFactory._field_defs == {}

    def test_meta_model_not_a_model(self) -> None:
        with pytest.raises(TypeError) as info:

            class Bad(ModelFactory):
                class Meta:
                    model = int

        assert str(info.value) == f"Bad.Meta.model must be a BaseSurrealModel subclass, not {int!r}"

    def test_unknown_declared_name(self) -> None:
        with pytest.raises(TypeError) as info:

            class UserFactory(ModelFactory[FxUser]):
                class Meta:
                    model = FxUser

                nmae = "x"

        assert str(info.value) == (
            "UserFactory declares 'nmae', which is not a field of FxUser (fields: age, email, id, name, role)"
        )

    def test_subclass_meta_without_model_keeps_the_parent_model(self) -> None:
        class Child(UserFactory):
            class Meta:
                pass

            age = 30

        assert Child._get_model() is FxUser
        assert Child._field_defs["role"] == "admin"
        assert Child._field_defs["age"] == 30

    def test_subclass_without_meta_keeps_the_parent_model(self) -> None:
        class Child(UserFactory):
            pass

        assert Child._get_model() is FxUser

    def test_abstract_base_checked_against_the_subclass_model(self) -> None:
        class Base(ModelFactory):
            role = "x"
            nmae = "y"

        assert Base._get_model() is None
        assert Base._field_defs == {"role": "x", "nmae": "y"}
        with pytest.raises(TypeError, match="'nmae', which is not a field of FxUser"):

            class Sub(Base):
                class Meta:
                    model = FxUser

    def test_abstract_base_with_valid_names(self) -> None:
        class Base(ModelFactory):
            role = "x"

        class Sub(Base):
            class Meta:
                model = FxUser

        assert Sub._field_defs == {"role": "x"}

    @pytest.mark.parametrize("declared", ["password_hash", "password"])
    def test_alias_normalized_to_the_python_name(self, declared: str) -> None:
        Factory = type("SecretFactory", (ModelFactory,), {"Meta": type("Meta", (), {"model": FxSecret}), declared: "a"})
        assert Factory._field_defs == {"password": "a"}

    def test_subclass_python_name_overrides_parent_column(self) -> None:
        class Parent(ModelFactory[FxSecret]):
            class Meta:
                model = FxSecret

            password_hash = "a"

        class Child(Parent):
            password = "b"

        assert Child._field_defs == {"password": "b"}

    def test_both_names_in_one_class_body(self) -> None:
        with pytest.raises(TypeError) as info:

            class SecretFactory(ModelFactory[FxSecret]):
                class Meta:
                    model = FxSecret

                password = "a"
                password_hash = "b"

        assert str(info.value) == "SecretFactory declares 'password' twice: as 'password' and as its column 'password_hash'"

    @pytest.mark.parametrize("endpoint", ["in_", "in"])
    def test_edge_endpoints(self, endpoint: str) -> None:
        rid = RecordID("fx_user", "a")
        Factory = type(
            "KnowsFactory",
            (ModelFactory,),
            {"Meta": type("Meta", (), {"model": FxKnows}), endpoint: rid, "out": rid, "since": 1},
        )
        assert Factory._field_defs == {"in_": rid, "out": rid, "since": 1}

    def test_edge_surreal_func_declared(self) -> None:
        with pytest.raises(TypeError) as info:

            class F(ModelFactory[FxKnows]):
                class Meta:
                    model = FxKnows

                since = SurrealFunc("time::now()")

        assert str(info.value) == (
            "F declares SurrealFunc 'since', but FxKnows is an edge: its save() (INSERT RELATION) cannot evaluate "
            "server values. Use DEFINE FIELD … DEFAULT on the edge table, or a plain value."
        )

    def test_edge_surreal_func_inherited(self) -> None:
        class Base(ModelFactory):
            since = SurrealFunc("time::now()")

        with pytest.raises(TypeError, match="F declares SurrealFunc 'since', but FxKnows is an edge"):

            class F(Base):
                class Meta:
                    model = FxKnows

    def test_edge_plain_value_overrides_inherited_surreal_func(self) -> None:
        class Base(ModelFactory):
            since = SurrealFunc("time::now()")

        class F(Base):
            class Meta:
                model = FxKnows

            since = 3

        assert F._field_defs == {"since": 3}

    def test_extra_allow_model_accepts_any_name(self) -> None:
        assert FxFree.model_config.get("extra") == "allow"

        class FreeFactory(ModelFactory[FxFree]):
            class Meta:
                model = FxFree

            anything = 1
            other = "x"

        assert FreeFactory._field_defs == {"anything": 1, "other": "x"}

    def test_node_surreal_func_collected(self) -> None:
        func = SurrealFunc("rand::int(18, 80)")

        class F(ModelFactory[FxUser]):
            class Meta:
                model = FxUser

            age = func

        assert F._field_defs == {"age": func}

    def test_methods_without_a_model_raise(self) -> None:
        class Abstract(ModelFactory):
            role = "x"

        text = re.escape("Abstract.Meta.model is not set. Define a Meta class with a model attribute.")
        with pytest.raises(ValueError, match=text):
            Abstract.build()
        with pytest.raises(ValueError, match=text):
            Abstract.build_batch(2)

    async def test_async_methods_without_a_model_raise(self) -> None:
        class Abstract(ModelFactory):
            pass

        text = re.escape("Abstract.Meta.model is not set. Define a Meta class with a model attribute.")
        with pytest.raises(ValueError, match=text):
            await Abstract.create()
        with pytest.raises(ValueError, match=text):
            await Abstract.create_batch(2)

    def test_typing_extensions_only_under_type_checking(self) -> None:
        source = Path(factories_module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        guarded: set[int] = set()
        for node in tree.body:
            if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
                guarded.update(id(child) for child in ast.walk(node))
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name.split(".")[0] == "typing_extensions" for name in names):
                found.append(node)
                assert id(node) in guarded, f"typing_extensions imported outside TYPE_CHECKING (line {node.lineno})"
        assert found, "the TYPE_CHECKING import of typing_extensions is gone: update this test"


_EDGE_SUFFIX = (
    "its save() (INSERT RELATION) cannot evaluate server values. Use DEFINE FIELD … DEFAULT on the edge table, "
    "or a plain value."
)


@pytest.fixture
def no_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any attempt to reach a database: ``build()`` must need none (AC-7)."""

    async def _refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("build() reached for a database client")

    monkeypatch.setattr(SurrealDBConnectionManager, "get_client", classmethod(_refuse))


@pytest.mark.usefixtures("no_connection")
class TestBuild:
    """``build()`` / ``build_batch()``: values, copies, override checks, names and the model call (AC-7/21/22)."""

    def test_declarations_and_model(self) -> None:
        user = UserFactory.build()
        assert type(user) is FxUser
        assert user.role == "admin"
        assert user.name in {f"{f} {la}" for f in _FULL_FIRST_NAMES for la in _FULL_LAST_NAMES}
        assert user.id is None
        assert user.age == 0

    def test_override_wins(self) -> None:
        user = UserFactory.build(name="Bob", role="guest", age=33)
        assert (user.name, user.role, user.age) == ("Bob", "guest", 33)

    def test_faker_override_generated(self) -> None:
        user = UserFactory.build(age=Faker("random_int", min=5, max=5))
        assert user.age == 5

    def test_static_values_deep_copied(self) -> None:
        rid = RecordID("fx_user", "a")
        address = FxAddress(city="Paris")

        class RichFactory(ModelFactory[FxRich]):
            class Meta:
                model = FxRich

            tags = ["a", "b"]
            owner = rid
            address = FxAddress(city="Paris")

        first = RichFactory.build()
        first.tags.append("c")
        assert first.address is not None
        first.address.city = "Lyon"
        second = RichFactory.build()
        assert second.tags == ["a", "b"]
        assert second.address == address
        assert RichFactory._field_defs["tags"] == ["a", "b"]
        assert RichFactory._field_defs["address"] == address
        assert second.owner == rid
        assert second.owner is not RichFactory._field_defs["owner"]
        assert second.address is not RichFactory._field_defs["address"]

    def test_override_is_used_as_is(self) -> None:
        address = FxAddress(city="Paris")

        class RichFactory(ModelFactory[FxRich]):
            class Meta:
                model = FxRich

        assert RichFactory.build(address=address).address is address

    def test_unknown_override(self) -> None:
        with pytest.raises(TypeError) as info:
            UserFactory.build(nmae="x")
        assert str(info.value) == "build() got 'nmae', which is not a field of FxUser (fields: age, email, id, name, role)"

    def test_unknown_override_batch_names_the_method(self) -> None:
        with pytest.raises(TypeError, match=r"^build_batch\(\) got 'nmae', which is not a field of FxUser"):
            UserFactory.build_batch(2, nmae="x")

    def test_extra_allow_model_accepts_any_override(self) -> None:
        class FreeFactory(ModelFactory[FxFree]):
            class Meta:
                model = FxFree

            anything = 1

        free = FreeFactory.build(other="x")
        assert free.model_extra == {"anything": 1, "other": "x"}

    def test_surreal_func_attribute_left_out(self) -> None:
        class SecretFactory(ModelFactory[FxSecret]):
            class Meta:
                model = FxSecret

            password = SurrealFunc("rand::string(12)")

        assert SecretFactory.build().password is None

    def test_surreal_func_attribute_on_a_required_field(self) -> None:
        class RequiredFactory(ModelFactory[FxRequired]):
            class Meta:
                model = FxRequired

            code = SurrealFunc("rand::string(12)")

        with pytest.raises(ValidationError):
            RequiredFactory.build()

    @pytest.mark.parametrize("method", ["build", "build_batch"])
    def test_surreal_func_override(self, method: str) -> None:
        with pytest.raises(TypeError) as info:
            if method == "build":
                UserFactory.build(age=SurrealFunc("rand::int(18, 80)"))
            else:
                UserFactory.build_batch(2, age=SurrealFunc("rand::int(18, 80)"))
        assert str(info.value) == f"{method}() cannot evaluate SurrealFunc 'age': it is generated by the server on create()"

    def test_invalid_value_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            UserFactory.build(age="not a number")

    def test_seed_reproduces_a_batch(self) -> None:
        random.seed(3)
        first = [user.model_dump() for user in UserFactory.build_batch(5)]
        random.seed(3)
        second = [user.model_dump() for user in UserFactory.build_batch(5)]
        assert first == second
        assert len({user["name"] for user in first}) > 1

    def test_batch_has_fresh_values_and_copies(self) -> None:
        class RichFactory(ModelFactory[FxRich]):
            class Meta:
                model = FxRich

            tags = ["a"]

        batch = RichFactory.build_batch(3, address=FxAddress(city="Paris"))
        assert len(batch) == 3
        assert len({id(item.tags) for item in batch}) == 3
        assert all(item.address == FxAddress(city="Paris") for item in batch)

    # -- names (D15), both directions --------------------------------------------------------------------------

    def test_override_by_python_name_beats_declared_column(self) -> None:
        class SecretFactory(ModelFactory[FxSecret]):
            class Meta:
                model = FxSecret

            password_hash = Faker("word")

        assert SecretFactory.build(password="x").password == "x"

    def test_override_by_column_beats_declared_python_name(self) -> None:
        class SecretFactory(ModelFactory[FxSecret]):
            class Meta:
                model = FxSecret

            password = Faker("word")

        assert SecretFactory.build(password_hash="x").password == "x"

    def test_both_names_in_one_call(self) -> None:
        class SecretFactory(ModelFactory[FxSecret]):
            class Meta:
                model = FxSecret

        with pytest.raises(TypeError) as info:
            SecretFactory.build(password="x", password_hash="y")
        assert str(info.value) == "build() got 'password' twice: as 'password' and as its column 'password_hash'"

    def test_plain_override_beats_declared_surreal_func(self) -> None:
        class SecretFactory(ModelFactory[FxSecret]):
            class Meta:
                model = FxSecret

            password_hash = SurrealFunc("rand::string(12)")

        assert SecretFactory.build(password="x").password == "x"

    def test_declared_alias_reaches_the_instance(self) -> None:
        class SecretFactory(ModelFactory[FxSecret]):
            class Meta:
                model = FxSecret

            password_hash = "declared"

        assert SecretFactory.build().password == "declared"
        assert SecretFactory.build(password_hash="override").password == "override"

    # -- edges (D14) -------------------------------------------------------------------------------------------

    def test_edge_build_with_endpoints(self) -> None:
        alice, bob = RecordID("fx_user", "alice"), RecordID("fx_user", "bob")

        class KnowsFactory(ModelFactory[FxKnows]):
            class Meta:
                model = FxKnows

            since = 2020

        knows = KnowsFactory.build(**{"in": alice}, out=bob)
        assert (knows.in_, knows.out, knows.since) == (alice, bob, 2020)

    @pytest.mark.parametrize("method", ["build", "build_batch"])
    def test_edge_surreal_func_override_checked_first(self, method: str) -> None:
        class KnowsFactory(ModelFactory[FxKnows]):
            class Meta:
                model = FxKnows

            since = 2020

        func = SurrealFunc("time::now()")
        with pytest.raises(TypeError) as info:
            if method == "build":
                KnowsFactory.build(since=func)
            else:
                KnowsFactory.build_batch(2, since=func)
        assert str(info.value) == f"{method}() got SurrealFunc 'since', but FxKnows is an edge: {_EDGE_SUFFIX}"

    # -- positional-only cls / count (D13) ---------------------------------------------------------------------

    def test_count_field_overridable_on_build_batch(self) -> None:
        class CountedFactory(ModelFactory[FxCounted]):
            class Meta:
                model = FxCounted

        batch = CountedFactory.build_batch(3, count=5)
        assert [item.count for item in batch] == [5, 5, 5]

    def test_count_and_cls_fields_overridable_on_build(self) -> None:
        class CountedFactory(ModelFactory[FxCounted]):
            class Meta:
                model = FxCounted

        built = CountedFactory.build(count=7, cls="x")
        assert (built.count, built.cls) == (7, "x")

    def test_count_by_keyword_is_a_type_error(self) -> None:
        with pytest.raises(TypeError):
            UserFactory.build_batch(count=3)  # type: ignore[call-arg]

    # -- P2: the key handed to the model -----------------------------------------------------------------------

    def test_by_column_model_measured(self) -> None:
        assert FxByColumn(pw="x").pw is None  # type: ignore[call-arg]
        assert FxByColumn(pw_hash="x").pw == "x"

    def test_by_column_model_declared_value(self) -> None:
        class ByColumnFactory(ModelFactory[FxByColumn]):
            class Meta:
                model = FxByColumn

            pw = "declared"

        assert ByColumnFactory.build().pw == "declared"

    @pytest.mark.parametrize("key", ["pw", "pw_hash"])
    def test_by_column_model_override_by_either_name(self, key: str) -> None:
        class ByColumnFactory(ModelFactory[FxByColumn]):
            class Meta:
                model = FxByColumn

            pw_hash = "declared"

        assert ByColumnFactory.build(**{key: "override"}).pw == "override"
        assert [item.pw for item in ByColumnFactory.build_batch(2, **{key: "override"})] == ["override", "override"]

    @pytest.mark.parametrize("key", ["password", "password_hash"])
    def test_by_name_model_override_by_either_name(self, key: str) -> None:
        class SecretFactory(ModelFactory[FxSecret]):
            class Meta:
                model = FxSecret

            password = "declared"

        assert SecretFactory.build().password == "declared"
        assert SecretFactory.build(**{key: "override"}).password == "override"

    def test_validate_by_name_takes_precedence(self) -> None:
        class FxByNameOnly(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="fx_byname", populate_by_name=False, validate_by_name=True)

            id: str | None = None
            pw: str | None = Field(None, alias="pw_hash")

        class F(ModelFactory[FxByNameOnly]):
            class Meta:
                model = FxByNameOnly

            pw_hash = "declared"

        assert F.build().pw == "declared"


@pytest.mark.usefixtures("no_connection")
class TestCount:
    """``count`` of ``build_batch()``: an ``int`` (not a ``bool``), ``>= 0``; ``0`` builds nothing (AC-8)."""

    def test_zero(self) -> None:
        assert UserFactory.build_batch(0) == []

    def test_negative(self) -> None:
        with pytest.raises(ValueError) as info:
            UserFactory.build_batch(-1)
        assert str(info.value) == "count must be >= 0, got -1"

    @pytest.mark.parametrize("count", [True, 2.0, "2", None])
    def test_not_an_int(self, count: Any) -> None:
        with pytest.raises(TypeError, match="count must be an int"):
            UserFactory.build_batch(count)

    def test_batch_size(self) -> None:
        assert len(UserFactory.build_batch(4)) == 4


class StampedFactory(ModelFactory[FxStamped]):
    """Server values on optional fields; ``token`` declared by its column ``tok`` (D15)."""

    class Meta:
        model = FxStamped

    name = Faker("name")
    age = SurrealFunc("rand::int(18, 80)")
    tok = SurrealFunc("<string> rand::uuid::v7()")
    joined = SurrealFunc("time::now()")


class CountedFactory(ModelFactory[FxCounted]):
    """A factory of a model with fields ``count`` and ``cls`` (D13)."""

    class Meta:
        model = FxCounted


class TypedFactory(ModelFactory[FxTyped]):
    """A ``date``, a ``datetime`` and a ``UUID`` generated by ``Faker``."""

    class Meta:
        model = FxTyped

    born = Faker("date")
    seen = Faker("datetime")
    ref = Faker("uuid")


class TestCreateServerValuesKeys:
    """``create()`` hands ``save()`` server values keyed by Python name only, never by column (AC-21)."""

    @pytest.fixture
    def recorded(self, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = []

        async def _save(self: BaseSurrealModel, *args: Any, **kwargs: Any) -> BaseSurrealModel:
            calls.append({"args": args, **kwargs})
            return self

        monkeypatch.setattr(FxStamped, "save", _save)
        return calls

    async def test_server_values_keyed_by_python_name(self, recorded: list[dict[str, Any]]) -> None:
        user = await StampedFactory.create()
        assert len(recorded) == 1
        server_values = recorded[0]["server_values"]
        assert set(server_values) == {"age", "token", "joined"}
        assert "tok" not in server_values
        assert all(isinstance(value, SurrealFunc) for value in server_values.values())
        assert recorded[0]["args"] == ()
        assert isinstance(user, FxStamped)

    async def test_plain_override_by_other_name_sends_no_server_value(self, recorded: list[dict[str, Any]]) -> None:
        class TokenOnly(ModelFactory[FxStamped]):
            class Meta:
                model = FxStamped

            tok = SurrealFunc("<string> rand::uuid::v7()")

        user = await TokenOnly.create(token="x")
        assert recorded == [{"args": ()}]
        assert user.token == "x"

    async def test_create_checks_overrides_as_create(self, recorded: list[dict[str, Any]]) -> None:
        with pytest.raises(TypeError, match=r"^create\(\) got 'nmae', which is not a field of FxStamped"):
            await StampedFactory.create(nmae="x")
        with pytest.raises(TypeError, match=r"^create_batch\(\) got 'token' twice"):
            await StampedFactory.create_batch(2, token="x", tok="y")
        assert recorded == []

    async def test_create_batch_count_checked_before_any_save(self, recorded: list[dict[str, Any]]) -> None:
        assert await StampedFactory.create_batch(0) == []
        with pytest.raises(ValueError, match="count must be >= 0, got -1"):
            await StampedFactory.create_batch(-1)
        with pytest.raises(TypeError, match="count must be an int"):
            await StampedFactory.create_batch(True)
        with pytest.raises(TypeError):
            await StampedFactory.create_batch(count=3)  # type: ignore[call-arg]
        assert recorded == []

    async def test_abstract_factory_create_raises(self) -> None:
        class Base(ModelFactory):
            name = Faker("name")

        with pytest.raises(ValueError, match=r"Base\.Meta\.model is not set"):
            await Base.create()
        with pytest.raises(ValueError, match=r"Base\.Meta\.model is not set"):
            await Base.create_batch(1)


def _key(record_id: Any) -> Any:
    """The record key of an id: ``save()`` keeps a ``str`` field's auto id as the key, a read gives a ``RecordID``."""
    return record_id.id if isinstance(record_id, RecordID) else record_id


def _same_record(saved: BaseSurrealModel, fetched: BaseSurrealModel) -> bool:
    """Whether a created instance and its read-back hold the same key and the same field values."""
    return _key(saved.get_id()) == _key(fetched.get_id()) and saved.model_dump(exclude={"id"}) == fetched.model_dump(
        exclude={"id"}
    )


class TestCreateE2E:
    """``create()``/``create_batch()`` save through ``save()`` on 3.x and 2.x (AC-9, AC-11, AC-22)."""

    async def test_create_round_trip(self) -> None:
        async with orm_client("fx_user", "fx_typed"):
            user = await UserFactory.create()
            assert user.get_id() is not None
            fetched = await FxUser.objects().get(user.get_id())
            assert _same_record(user, fetched)
            assert fetched.role == "admin"

            typed = await TypedFactory.create()
            assert isinstance(typed.born, date)
            assert isinstance(typed.seen, datetime)
            assert isinstance(typed.ref, uuid.UUID)
            assert _same_record(typed, await FxTyped.objects().get(typed.get_id()))

    async def test_explicit_id_kept_and_duplicate_refused(self) -> None:
        async with orm_client("fx_user"):
            user = await UserFactory.create(id="same", name="Ada")
            assert user.id == "same"
            fetched = await FxUser.objects().get("same")
            assert fetched.name == "Ada"
            with pytest.raises(SurrealDbError):
                await UserFactory.create(id="same")

    async def test_signals_fired_once_per_record(self) -> None:
        seen: list[str] = []

        async def _pre(sender: Any, instance: Any, **kwargs: Any) -> None:
            seen.append(f"pre:{instance.name}")

        async def _post(sender: Any, instance: Any, **kwargs: Any) -> None:
            seen.append(f"post:{instance.name}")

        pre_save.connect(FxUser)(_pre)
        post_save.connect(FxUser)(_post)
        try:
            async with orm_client("fx_user"):
                await UserFactory.create(name="a")
                await UserFactory.create_batch(2, name="b")
        finally:
            pre_save.disconnect(_pre, FxUser)
            post_save.disconnect(_post, FxUser)
        assert seen == ["pre:a", "post:a", "pre:b", "post:b", "pre:b", "post:b"]

    async def test_create_batch_rows(self) -> None:
        async with orm_client("fx_user"):
            users = await UserFactory.create_batch(3)
            assert len(users) == 3
            assert len({_key(u.get_id()) for u in users}) == 3
            assert await FxUser.objects().count() == 3

    async def test_create_batch_positional_count(self) -> None:
        async with orm_client("fx_counted"):
            rows = await CountedFactory.create_batch(2, count=5, cls="x")
            assert [(r.count, r.cls) for r in rows] == [(5, "x"), (5, "x")]
            stored = await FxCounted.objects().all()
            assert sorted((r.count, r.cls) for r in stored) == [(5, "x"), (5, "x")]

    async def test_create_batch_zero_makes_no_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _refuse(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("create_batch(0) called save()")

        monkeypatch.setattr(FxUser, "save", _refuse)
        assert await UserFactory.create_batch(0) == []

    async def test_failing_record_stops_batch_and_keeps_earlier(self, monkeypatch: pytest.MonkeyPatch) -> None:
        original = FxUser.save
        calls = 0

        async def _third_fails(self: FxUser, *args: Any, **kwargs: Any) -> FxUser:
            nonlocal calls
            calls += 1
            if calls == 3:
                raise SurrealDbError("third save refused")
            return await original(self, *args, **kwargs)

        monkeypatch.setattr(FxUser, "save", _third_fails)
        async with orm_client("fx_user"):
            with pytest.raises(SurrealDbError, match="third save refused"):
                await UserFactory.create_batch(5)
            assert calls == 3
            assert await FxUser.objects().count() == 2


_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$")


class TestServerValuesE2E:
    """``SurrealFunc`` declarations and overrides of a node factory, generated by the server (AC-10, AC-21)."""

    async def test_declared_server_values(self) -> None:
        async with orm_client("fx_stamped"):
            first, second = await StampedFactory.create_batch(2)
            for record in (first, second):
                assert record.age is not None and 18 <= record.age <= 80
                assert record.token is not None and _UUID_RE.match(record.token)
                assert isinstance(record.joined, datetime)
                stored = await FxStamped.objects().get(record.get_id())
                assert (stored.age, stored.token, stored.joined) == (record.age, record.token, record.joined)
            assert first.token != second.token

    async def test_plain_override_sent_as_data(self) -> None:
        async with orm_client("fx_stamped"):
            record = await StampedFactory.create(age=30)
            assert record.age == 30
            assert (await FxStamped.objects().get(record.get_id())).age == 30

    async def test_server_value_override_on_create(self) -> None:
        async with orm_client("fx_stamped"):
            record = await StampedFactory.create(age=SurrealFunc("rand::int(1, 2)"))
            assert record.age in (1, 2)
            assert (await FxStamped.objects().get(record.get_id())).age in (1, 2)

    @pytest.mark.parametrize("key", ["token", "tok"])
    async def test_plain_override_beats_declared_under_either_name(self, key: str) -> None:
        async with orm_client("fx_stamped"):
            record = await StampedFactory.create(**{key: "fixed"})
            assert record.token == "fixed"
            assert (await FxStamped.objects().get(record.get_id())).token == "fixed"


# --- edges, named connections, views and the embedded engine (AC-12, AC-13, AC-14) ----------------------------------


class FxKnowsFactory(ModelFactory[FxKnows]):
    """An edge factory: a plain ``since``, endpoints given at the call."""

    class Meta:
        model = FxKnows

    since = Faker("random_int")


class FxUserView(BaseSurrealModel):
    """A materialized view over ``fx_user``: its writes raise ``TypeError`` before any request."""

    model_config = SurrealConfigDict(table_name="fx_user_view", view_query="SELECT name FROM fx_user")

    id: RecordID | None = None
    name: str = ""


class FxUserViewFactory(ModelFactory[FxUserView]):
    """A factory of a materialized view model: ``build()`` works, ``create()`` is refused by ``save()``."""

    class Meta:
        model = FxUserView

    name = Faker("name")


def _rid(user: FxUser) -> RecordID:
    """The record id of a created ``FxUser`` (``save()`` keeps a ``str`` id field's key)."""
    return RecordID("fx_user", _key(user.get_id()))


async def _edge_count(client: Any) -> int:
    """The rows of ``fx_knows``, counted by the server (``0`` for an absent table)."""
    rows = await client.query("SELECT count() FROM fx_knows GROUP ALL;", {})
    return int(rows[0]["count"]) if rows else 0


async def _tables(client: Any) -> set[str]:
    """The tables of the current database (``INFO FOR DB``)."""
    info = await client.query("INFO FOR DB;", {})
    return set(info.get("tables", {}))


class TestEdgeFactoryE2E:
    """An edge factory relates factory-created nodes, on 3.x and 2.x (AC-12)."""

    async def test_edges_between_factory_nodes_are_traversed(self) -> None:
        async with orm_client("fx_user", "fx_knows") as client:
            a = await UserFactory.create(id="a", name="A")
            b = await UserFactory.create(id="b", name="B")
            c = await UserFactory.create(id="c", name="C")
            first = await FxKnowsFactory.create(in_=_rid(a), out=_rid(b))
            second = await FxKnowsFactory.create(**{"in": _rid(a)}, out=_rid(c))
            for edge in (first, second):
                assert isinstance(edge.since, int)
                assert edge.in_ == _rid(a)
            assert (first.out, second.out) == (_rid(b), _rid(c))
            rows = await client.query("SELECT ->fx_knows->fx_user AS friends FROM fx_user:a;", {})
            assert {_key(friend) for friend in rows[0]["friends"]} == {"b", "c"}
            assert await _edge_count(client) == 2

    async def test_surreal_func_override_refused_before_any_request(self) -> None:
        async with orm_client("fx_user", "fx_knows") as client:
            a = await UserFactory.create(id="a")
            b = await UserFactory.create(id="b")
            await FxKnowsFactory.create(in_=_rid(a), out=_rid(b), since=7)
            with pytest.raises(TypeError) as info:
                await FxKnowsFactory.create(in_=_rid(a), out=_rid(b), since=SurrealFunc("time::now()"))
            assert str(info.value) == f"create() got SurrealFunc 'since', but FxKnows is an edge: {_EDGE_SUFFIX}"
            with pytest.raises(TypeError, match=r"^create_batch\(\) got SurrealFunc 'since'"):
                await FxKnowsFactory.create_batch(2, in_=_rid(a), out=_rid(b), since=SurrealFunc("time::now()"))
            assert await _edge_count(client) == 1


#: The database of ``TestFactoryRoutingE2E``'s named connection: a constant of this module, never input.
_OTHER_DATABASE = "fx_factory_other"


@contextlib.asynccontextmanager
async def _other_connection(client: Any) -> AsyncIterator[str]:
    """Register ``"fx_other"`` on ``ns``/``fx_factory_other`` of the line under test, removed before and after.

    *client* is the default connection's: it runs ``REMOVE DATABASE IF EXISTS`` (and ``DEFINE DATABASE``) on entry
    and ``REMOVE DATABASE IF EXISTS`` on exit — the pattern of ``tests.conftest.change_feed_database()`` — so nothing
    of a run survives into the next one.
    """
    await apply_reset(client, f"REMOVE DATABASE IF EXISTS {_OTHER_DATABASE}; DEFINE DATABASE {_OTHER_DATABASE};")
    SurrealDBConnectionManager.add_connection(
        "fx_other", url=_ws_url(), user="root", password="root", namespace="ns", database=_OTHER_DATABASE
    )
    try:
        yield "fx_other"
    finally:
        await SurrealDBConnectionManager.remove_connection("fx_other")
        with contextlib.suppress(Exception):
            await client.query(f"REMOVE DATABASE IF EXISTS {_OTHER_DATABASE};", {})


class TestFactoryRoutingE2E:
    """``create()`` follows ``using()``; a materialized view's ``create()`` is refused (AC-13)."""

    async def test_create_inside_using_writes_to_that_database_only(self) -> None:
        async with orm_client("fx_user") as client:
            for run in (1, 2):  # the second run finds an empty database: nothing survives the helper
                async with _other_connection(client) as name:
                    async with SurrealDBConnectionManager.using(name):
                        assert await FxUser.objects().count() == 0, f"run {run}"
                        user = await UserFactory.create(name=f"routed-{run}")
                    async with SurrealDBConnectionManager.using(name):
                        fetched = await FxUser.objects().get(user.get_id())
                        assert fetched.name == f"routed-{run}"
                        assert await FxUser.objects().count() == 1
                    assert await FxUser.objects().count() == 0
            remaining = await client.query("INFO FOR NS;", {})
            assert _OTHER_DATABASE not in remaining.get("databases", {})

    async def test_view_factory_builds_but_create_is_refused(self) -> None:
        view = FxUserViewFactory.build()
        assert isinstance(view, FxUserView) and view.name
        async with orm_client("fx_user", "fx_user_view") as client:
            with pytest.raises(TypeError, match="FxUserView"):
                await FxUserViewFactory.create()
            with pytest.raises(TypeError, match="FxUserView"):
                await FxUserViewFactory.create_batch(2)
            assert "fx_user_view" not in await _tables(client)


@fixture
class FxFixtures(SurrealFixture):
    """A ``SurrealFixture`` whose templates come from ``build()`` (§5.3)."""

    a = UserFactory.build(id="a")
    b = UserFactory.build(id="b")


@pytest.mark.skipif(not _embedded_engine_available(), reason="the installed surrealdb SDK has no embedded engine")
class TestFactoriesEmbeddedE2E:
    """Factories inside ``embedded_database()``, and as ``SurrealFixture`` templates (AC-14)."""

    async def test_create_batch_on_the_embedded_engine(self) -> None:
        async with embedded_database():
            users = await UserFactory.create_batch(4)
            assert len(users) == 4
            assert await FxUser.objects().count() == 4
            assert all(user.role == "admin" for user in await FxUser.objects().all())

    async def test_fixture_from_built_templates_loads_and_cleans_up(self) -> None:
        async with embedded_database():
            async with FxFixtures.load() as data:
                assert await FxUser.objects().count() == 2
                assert {_key(data.a.get_id()), _key(data.b.get_id())} == {"a", "b"}
                assert (await FxUser.objects().get("a")).role == "admin"
            assert await FxUser.objects().count() == 0
