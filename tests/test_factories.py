"""``surreal_orm_lite.testing`` model factories (v0.40.0): ``Faker`` and ``ModelFactory``, mirroring the full ORM.

``Faker`` generates test data with the full SurrealDB-ORM's fourteen providers, pools and output formats; the lite
checks a provider and its keywords when the ``Faker`` is built rather than when it generates. ``ModelFactory``
collects its declarations when the class is created and checks them against ``Meta.model`` there. The classes that
open a server or the embedded engine are suffixed ``E2E``.
"""

import ast
import enum
import functools
import random
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field
from surrealdb import RecordID

import surreal_orm_lite.testing.factories as factories_module
from surreal_orm_lite import BaseSurrealEdge, BaseSurrealModel, SurrealConfigDict, SurrealFunc
from surreal_orm_lite.testing.factories import Faker, ModelFactory


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
