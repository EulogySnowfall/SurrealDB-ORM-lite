"""Migration system (v0.38.0): the comparator of ``migrations/compare.py``.

``compare.literal_equal(a, b)`` (``TestLiteralEqual``) compares two SurrealQL ``DEFAULT`` texts: two literals the ORM
can read by their value and kind — a float with or without its ``f`` suffix, a decimal however many trailing zeros the
server keeps (M15 b, c), a string however quoted —, anything else by its text. Unit tests only (no server).
"""

import pytest

from surreal_orm_lite.migrations import compare


class TestLiteralEqual:
    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("1.5", "1.5f"),
            ("1.10dec", "1.1dec"),
            ("18", "18"),
            ("-3", "-3"),
            ("true", "true"),
            ("'a;b'", "'a;b'"),
            ('"it\'s"', "'it\\'s'"),
            ("d'2026-01-01T00:00:00Z'", "d'2026-01-01T00:00:00.000000Z'"),
            ("NONE", "NONE"),
            ("time::now()", "time::now()"),
            (" 18 ", "18"),
        ],
    )
    def test_equal(self, a: str, b: str) -> None:
        assert compare.literal_equal(a, b) is True
        assert compare.literal_equal(b, a) is True

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("1.5", "1.6f"),
            ("'a'", "'a '"),
            ("1", "1.0"),
            ("1", "1dec"),
            ("1.5f", "1.5dec"),
            ("true", "1"),
            ("false", "0"),
            ("'1'", "1"),
            ("d'2026-01-01T00:00:00Z'", "d'2026-01-01T00:00:01Z'"),
            ("NONE", "NULL"),
            ("time::now()", "time::now( )"),
            ("18", "time::now()"),
        ],
    )
    def test_different(self, a: str, b: str) -> None:
        assert compare.literal_equal(a, b) is False
        assert compare.literal_equal(b, a) is False
