"""Geometry types (v0.24.0): the ORM's geometry value classes and their error.

Unit tests first (no database); the raw-SDK E2E classes that pin what the servers do with each
value are suffixed ``E2E``. The reference values below are shared with ``tests/test_geo_fields.py``.
"""

from decimal import Decimal

import pytest
import surrealdb

import surreal_orm_lite
from surreal_orm_lite import InvalidGeometryError

# Reference points as (longitude, latitude).
A: tuple[float, float] = (-73.98, 40.74)
B: tuple[float, float] = (-73.99, 40.75)
# ``geo::distance`` of A and B, in metres, on SurrealDB 2.7.0, 3.1.5 and 3.3.4.
AB_METRES = 1395.0401142718986
TIMES_SQUARE: tuple[float, float] = (-73.9855, 40.7580)
# 697.5104962226121 metres from B.
NEAR: tuple[float, float] = (-73.985, 40.745)
PARIS: tuple[float, float] = (2.35, 48.85)
# A midtown box: A, B and NEAR are inside it, PARIS is outside.
RING: list[tuple[float, float]] = [(-74.0, 40.7), (-73.9, 40.7), (-73.9, 40.8), (-74.0, 40.8), (-74.0, 40.7)]
HOLE: list[tuple[float, float]] = [
    (-73.995, 40.745),
    (-73.990, 40.745),
    (-73.990, 40.750),
    (-73.995, 40.750),
    (-73.995, 40.745),
]
# Decimal coordinates of an SDK geometry and the float SurrealDB stores for each (every line).
DECIMAL_STORED: dict[Decimal, float] = {
    Decimal("1E-7"): 1e-07,
    Decimal("1.5E-7"): 1.5e-07,
    Decimal("-2.5E-7"): -2.5e-07,
    Decimal("1E+2"): 100.0,
    Decimal("1.2E+2"): 120.0,
    Decimal("1.234567890123456789012345678901E-7"): 1.2345678901234568e-07,
    Decimal("9.99999999999999999999999999999E-7"): 1e-06,
    Decimal("7.9228162514264337593543950335E+28"): 7.922816251426434e28,
    Decimal("1E-30"): 0.0,
    Decimal("0.1234567890123456789012345678901"): 0.12345678901234569,
}


class TestInvalidGeometryError:
    """AC-2: the ORM's geometry refusal is a ``ValueError`` and the SDK's ``InvalidGeometryError``."""

    def test_is_a_value_error_and_the_sdk_class(self) -> None:
        assert issubclass(InvalidGeometryError, ValueError)
        assert issubclass(InvalidGeometryError, surrealdb.InvalidGeometryError)
        assert InvalidGeometryError.__name__ == "InvalidGeometryError"
        assert InvalidGeometryError.__module__ == "surreal_orm_lite.exceptions"

    def test_message(self) -> None:
        assert str(InvalidGeometryError("x")) == "x"

    def test_caught_by_code_written_against_the_sdk(self) -> None:
        with pytest.raises(surrealdb.InvalidGeometryError):
            raise InvalidGeometryError("x")
        with pytest.raises(ValueError):
            raise InvalidGeometryError("x")

    def test_exported(self) -> None:
        assert "InvalidGeometryError" in surreal_orm_lite.__all__
        assert surreal_orm_lite.InvalidGeometryError is surreal_orm_lite.exceptions.InvalidGeometryError
