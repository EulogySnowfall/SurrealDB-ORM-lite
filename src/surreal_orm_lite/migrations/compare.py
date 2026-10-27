"""The normalising comparator of the migration system (v0.38.0) — private.

Every equality between two definitions goes through this module, so a definition the ORM wrote and the same one as a
SurrealDB server renders it back compare equal. This part reads ``DEFAULT`` literals: :func:`read_literal` turns the
text of a literal the ORM can write — a ``bool``, an ``int``, a finite ``float`` (``1.5`` or ``1.5f``), a ``decimal``
(``1.10dec``), a string literal however quoted, a UTC datetime ``d'…Z'`` with at most six fractional digits — into its
Python value; :func:`literal_equal` compares two texts by that value and its kind (``1.5`` equals ``1.5f`` and
``1.10dec`` equals ``1.1dec``, M15 b and c), any other text by the text itself.

Imports no other module of the ``migrations`` package: ``render`` imports this one.
"""

import math
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from ..define_parser import DefineParseError, unquote

__all__ = ["LiteralValue", "literal_equal", "read_literal"]

#: The value of a literal the ORM can write, as :func:`read_literal` returns it.
LiteralValue = bool | int | float | Decimal | str | datetime

_INTEGER = re.compile(r"-?[0-9]+")
#: A float as the ORM writes it (``1.5``) or as the servers render it (``1.5f``); ``2f`` too.
_FLOAT = re.compile(r"-?[0-9]+(?:\.[0-9]+f?|f)")
_DECIMAL = re.compile(r"(-?[0-9]+(?:\.[0-9]+)?)dec")
#: A UTC datetime as ``to_surql_literal()`` writes it: seconds, then at most six fractional digits.
_DATETIME = re.compile(r"d'([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?)Z'")


def read_literal(text: str) -> LiteralValue | None:
    """The Python value of the SurrealQL literal *text* (stripped), or ``None`` when it is not one of these forms:

    - ``true``, ``false``;
    - ``-?[0-9]+`` → ``int``;
    - ``-?[0-9]+.[0-9]+`` with or without the ``f`` suffix, or ``-?[0-9]+f`` → a finite ``float``;
    - ``-?[0-9]+(.[0-9]+)?dec`` → ``Decimal``, its digits kept (``1.10dec`` → ``Decimal('1.10')``);
    - ``'…'`` or ``"…"`` → ``str`` (``define_parser.unquote()``);
    - ``d'YYYY-MM-DDTHH:MM:SS[.ffffff]Z'`` (one to six fractional digits) → an aware UTC ``datetime``.

    ``NONE``, ``NULL``, arrays, objects, function calls, other prefixed strings (``u'…'``) and every expression give
    ``None``. Pure.
    """
    value = text.strip()
    if value in ("true", "false"):
        return value == "true"
    if _INTEGER.fullmatch(value):
        return int(value)
    if _FLOAT.fullmatch(value):
        number = float(value.removesuffix("f"))
        return number if math.isfinite(number) else None
    decimal = _DECIMAL.fullmatch(value)
    if decimal is not None:
        try:
            return Decimal(decimal.group(1))
        except InvalidOperation:  # pragma: no cover - the pattern only admits decimal digits
            return None
    if value[:1] in ("'", '"'):
        try:
            return unquote(value)
        except DefineParseError:
            return None
    moment = _DATETIME.fullmatch(value)
    if moment is not None:
        seconds, _, fraction = moment.group(1).partition(".")
        try:
            parsed = datetime.strptime(seconds, "%Y-%m-%dT%H:%M:%S")
        except ValueError:  # a date that does not exist: 2026-13-01
            return None
        return parsed.replace(microsecond=int(fraction.ljust(6, "0")) if fraction else 0, tzinfo=UTC)
    return None


def literal_equal(a: str, b: str) -> bool:
    """Whether the ``DEFAULT`` texts *a* and *b* mean the same value.

    Two literals :func:`read_literal` reads are equal when their values are of the same kind and equal: ``1.5`` and
    ``1.5f``, ``1.10dec`` and ``1.1dec``, ``"it's"`` and ``'it\\'s'`` are equal; ``1`` and ``1.0``, ``1`` and
    ``1dec``, ``true`` and ``1``, ``'a'`` and ``'a '`` are not. Any other pair compares its texts, stripped: ``NONE``
    equals ``NONE``, an expression equals itself only. Pure.
    """
    left, right = read_literal(a), read_literal(b)
    if left is not None and right is not None:
        return type(left) is type(right) and left == right
    return a.strip() == b.strip()
