"""Render Python values as SurrealQL literals — for the one place parameters cannot reach (v0.20.0).

SurrealDB 2.x does not carry bound parameters into a live query: ``LIVE SELECT … WHERE status =
$s`` is accepted, then evaluates ``$s`` as ``NONE`` at notification time and matches nothing,
without an error. Writing the value into the statement as a literal is the only form both
server lines honour, and it is what the full SurrealDB-ORM's custom SDK does too. Everywhere
else the ORM keeps binding parameters.

The encoder is deliberately **closed**: a type without a verified literal form raises
``TypeError`` instead of being stringified, because a guessed literal that parses but means
something else is a filter that silently watches the wrong records. Every form below was
checked as ``RETURN <literal> == $p`` against SurrealDB 2.7.0, 3.3.0 and 3.1.5, and
``tests/test_surql_literal.py`` re-checks it on every run.

Two choices worth knowing:

* Strings use single quotes with backslash and quote escaped — backslash first, so an escape
  already in the value can never combine with one the encoder adds.
* Record ids use the backtick form on both lines (table and string key each in backticks). The
  angle-bracket form is not used: escaping a closing bracket inside it parses on 2.x and is a
  syntax error on 3.x.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from ._sdk import RecordID

__all__ = ["inline_variables", "to_surql_literal"]

_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1

# A ``$name`` reference. Greedy, so ``$_f10`` is never read as ``$_f1`` followed by ``0``.
_REFERENCE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")

_SUPPORTED = "None, bool, int, float, str, Decimal, datetime, UUID, RecordID, and lists or dicts of these"


def _reject_nul(text: str) -> str:
    if "\x00" in text:
        raise TypeError(
            "A value containing a NUL character (\\x00) cannot be written into SurrealQL source — "
            "both server lines reject it — so it cannot be used in a live query filter."
        )
    return text


def _string(text: str) -> str:
    escaped = _reject_nul(text).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _identifier(text: str) -> str:
    escaped = _reject_nul(text).replace("\\", "\\\\").replace("`", "\\`")
    return f"`{escaped}`"


def _record_id(value: RecordID) -> str:
    key = value.id
    if isinstance(key, str):
        id_part = _identifier(key)
    elif (isinstance(key, int) and not isinstance(key, bool)) or isinstance(key, list | tuple | Mapping):
        id_part = to_surql_literal(key)
    else:
        raise TypeError(f"Cannot inline a record id whose key is a {type(key).__name__}; supported keys: str, int, list, dict.")
    return f"{_identifier(value.table_name)}:{id_part}"


def to_surql_literal(value: Any) -> str:
    """Return the SurrealQL literal that means exactly *value*.

    :raises TypeError: for a type with no verified literal form, a string containing ``\\x00``,
        an ``int`` outside signed 64-bit, a non-finite ``Decimal`` or a non-``str`` dict key.
    """
    if value is None:
        return "NONE"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        if not _INT64_MIN <= value <= _INT64_MAX:
            raise TypeError(f"Cannot inline {value}: SurrealDB integers are signed 64-bit.")
        return int.__repr__(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "math::inf" if value > 0 else "-math::inf"
        return float.__repr__(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise TypeError(f"Cannot inline Decimal({str(value)!r}): only finite decimals have a literal form.")
        return f"{format(value, 'f')}dec"
    if isinstance(value, datetime):
        text = value.isoformat()
        # A naive datetime is what the SDK itself sends as UTC; say so explicitly.
        return f"d'{text}'" if value.tzinfo is not None else f"d'{text}Z'"
    if isinstance(value, UUID):
        return f"u'{value}'"
    if isinstance(value, RecordID):
        return _record_id(value)
    if isinstance(value, list | tuple | set | frozenset):
        return "[" + ", ".join(to_surql_literal(item) for item in value) + "]"
    if isinstance(value, Mapping):
        parts = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"Cannot inline a dict with a {type(key).__name__} key; object keys must be str.")
            parts.append(f"{_string(key)}: {to_surql_literal(item)}")
        return "{" + ", ".join(parts) + "}"
    raise TypeError(
        f"Cannot inline a {type(value).__name__} value into a live query filter. SurrealDB 2.x "
        f"ignores bound parameters there, so the ORM writes each value as a SurrealQL literal, "
        f"and {type(value).__name__} has no verified literal form. Supported: {_SUPPORTED}."
    )


def inline_variables(sql: str, variables: Mapping[str, Any]) -> str:
    """Replace every bound ``$name`` in *sql* with the literal of its value, in one pass.

    The replacement is a callable, so the rendered text is inserted verbatim (``re.sub`` would
    otherwise read backslashes in a replacement *string* as escapes) and is never scanned again
    (a value that spells ``$other`` stays a string). A reference with no entry in *variables* —
    ``$auth``, ``$session``, a ``Var`` the caller did not supply — is left as written. Only the
    referenced values are rendered, so an unused variable of an unsupported type is harmless.

    :raises TypeError: naming the variable, if a referenced value cannot be inlined.
    """

    def render(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in variables:
            return match.group(0)
        try:
            return to_surql_literal(variables[name])
        except TypeError as exc:
            raise TypeError(f"${name}: {exc}") from exc

    return _REFERENCE.sub(render, sql)
