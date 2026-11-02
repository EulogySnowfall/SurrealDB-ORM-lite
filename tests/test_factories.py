"""``surreal_orm_lite.testing`` model factories (v0.40.0): ``Faker`` and ``ModelFactory``, mirroring the full ORM.

``Faker`` generates test data with the full SurrealDB-ORM's fourteen providers, pools and output formats; the lite
checks a provider and its keywords when the ``Faker`` is built rather than when it generates. The classes that open
a server or the embedded engine are suffixed ``E2E``.
"""

import random
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from surreal_orm_lite.testing.factories import Faker

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
