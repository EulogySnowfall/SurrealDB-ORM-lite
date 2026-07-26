"""Server-side computation helpers (v0.13.0).

``SurrealFunc`` marks a value as a **raw SurrealQL expression** to be evaluated by the
server instead of being sent as data. It is the building block of ``server_values=`` on
:meth:`BaseSurrealModel.save` / :meth:`BaseSurrealModel.merge`.

This module deliberately imports nothing from the rest of the package (``utils`` imports
*it*), so it stays cycle-free.
"""

from enum import StrEnum
from typing import Any

__all__ = [
    "SurrealArrayFunction",
    "SurrealCryptoFunction",
    "SurrealFunc",
    "SurrealFunction",
    "SurrealMathFunction",
    "SurrealRandFunction",
    "SurrealStringFunction",
    "SurrealTimeFunction",
]


class SurrealFunc:
    """A raw SurrealQL expression evaluated **server-side**.

    Wrap an expression to have it inlined into the generated statement instead of bound
    as data. The ORM compiles ``server_values={"joined_at": SurrealFunc("time::now()")}``
    to ``… SET joined_at = time::now()``, so the value is computed by SurrealDB (the DB
    clock, the server's crypto, …) rather than by Python::

        from surreal_orm_lite import SurrealFunc

        await player.save(server_values={"joined_at": SurrealFunc("time::now()")})

    To feed user input to an expression, reference a bound parameter and pass it through
    ``extra_vars`` — never format it into the expression string::

        await user.save(
            server_values={"password_hash": SurrealFunc("crypto::argon2::generate($password)")},
            extra_vars={"password": raw_password},   # bound, never interpolated
        )

    Warning:
        The expression is inserted **verbatim** into the query. Only build a
        ``SurrealFunc`` from developer-controlled text, never from user input. The
        rejection of ``;`` below is a guard against accidental statement chaining, **not**
        a sanitizer: parameter binding (regular field values and ``extra_vars``) is the
        injection boundary.
    """

    __slots__ = ("expression",)

    def __init__(self, expression: str) -> None:
        if not isinstance(expression, str):
            raise TypeError(f"SurrealFunc expression must be a string, got {type(expression).__name__!r}.")
        stripped = expression.strip()
        if not stripped:
            raise ValueError("SurrealFunc expression cannot be empty.")
        if ";" in stripped:
            raise ValueError(
                f"SurrealFunc expression may not contain ';' (statement terminator): {expression!r}. "
                "Pass a single expression; use extra_vars for values."
            )
        self.expression = stripped

    @classmethod
    def call(cls, fn: "str | SurrealFunction", *args: Any) -> "SurrealFunc":
        """Build ``fn(arg, ...)`` from a function name and raw SurrealQL argument fragments.

        ``fn`` is a plain string or any :class:`SurrealFunction` member (its value is the
        function name)::

            SurrealFunc.call(SurrealTimeFunction.NOW)                      # time::now()
            SurrealFunc.call(SurrealCryptoFunction.ARGON2_GENERATE, "$pw")  # crypto::argon2::generate($pw)

        Each argument is a **raw SurrealQL fragment** (a bound-parameter reference like
        ``"$password"``, a field name, or a literal such as ``"'a'"`` / ``"1w"``) and
        carries the same developer-controlled trust model as the expression itself.
        """
        rendered = ", ".join(str(arg) for arg in args)
        return cls(f"{fn}({rendered})")

    def __repr__(self) -> str:
        return f"SurrealFunc({self.expression!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SurrealFunc):
            return self.expression == other.expression
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.expression)


class SurrealFunction(StrEnum):
    """Base class for the curated SurrealQL function-name enums.

    Members are plain strings (the function name, e.g. ``"time::now"``), so they can be
    passed straight to :meth:`SurrealFunc.call` or interpolated into an expression.
    Subclass it to catalog functions the shipped enums don't cover.

    Every member of the shipped subclasses is **verified to resolve on both SurrealDB
    2.6.x and 3.x** by an E2E test that calls each one on both server lines. Functions
    whose name differs between the lines are deliberately left out (``rand::guid`` is
    2.6-only; ``type::is::*`` was renamed to ``type::is_*`` in 3.x) — pass those as a
    plain string if you target a single line. The enums are DX, not a gate:
    :class:`SurrealFunc` accepts any expression.
    """


class SurrealTimeFunction(SurrealFunction):
    """``time::*`` functions (https://surrealdb.com/docs/surrealql/functions/database/time)."""

    NOW = "time::now"
    CEIL = "time::ceil"
    FLOOR = "time::floor"
    ROUND = "time::round"
    GROUP = "time::group"
    UNIX = "time::unix"
    DAY = "time::day"
    MONTH = "time::month"
    YEAR = "time::year"
    HOUR = "time::hour"
    MINUTE = "time::minute"
    SECOND = "time::second"
    WDAY = "time::wday"
    WEEK = "time::week"
    YDAY = "time::yday"
    MIN = "time::min"
    MAX = "time::max"


class SurrealMathFunction(SurrealFunction):
    """``math::*`` functions (https://surrealdb.com/docs/surrealql/functions/database/math)."""

    ABS = "math::abs"
    CEIL = "math::ceil"
    FLOOR = "math::floor"
    ROUND = "math::round"
    SQRT = "math::sqrt"
    POW = "math::pow"
    MEAN = "math::mean"
    MEDIAN = "math::median"
    SUM = "math::sum"
    PRODUCT = "math::product"
    MIN = "math::min"
    MAX = "math::max"
    FIXED = "math::fixed"
    STDDEV = "math::stddev"
    VARIANCE = "math::variance"


class SurrealStringFunction(SurrealFunction):
    """``string::*`` functions (https://surrealdb.com/docs/surrealql/functions/database/string)."""

    CONCAT = "string::concat"
    LOWERCASE = "string::lowercase"
    UPPERCASE = "string::uppercase"
    TRIM = "string::trim"
    LEN = "string::len"
    SLUG = "string::slug"
    REPLACE = "string::replace"
    REVERSE = "string::reverse"
    SPLIT = "string::split"
    JOIN = "string::join"
    STARTS_WITH = "string::starts_with"
    ENDS_WITH = "string::ends_with"
    CONTAINS = "string::contains"
    REPEAT = "string::repeat"


class SurrealArrayFunction(SurrealFunction):
    """``array::*`` functions (https://surrealdb.com/docs/surrealql/functions/database/array)."""

    APPEND = "array::append"
    CONCAT = "array::concat"
    ADD = "array::add"
    COMPLEMENT = "array::complement"
    DISTINCT = "array::distinct"
    LEN = "array::len"
    REVERSE = "array::reverse"
    SORT = "array::sort"
    FLATTEN = "array::flatten"
    FIRST = "array::first"
    LAST = "array::last"
    MIN = "array::min"
    MAX = "array::max"


class SurrealCryptoFunction(SurrealFunction):
    """``crypto::*`` functions (https://surrealdb.com/docs/surrealql/functions/database/crypto).

    The ``*_GENERATE`` / ``*_COMPARE`` pairs are the reason ``extra_vars`` exists: hash a
    password server-side without the raw value ever being interpolated into the query.
    """

    ARGON2_GENERATE = "crypto::argon2::generate"
    ARGON2_COMPARE = "crypto::argon2::compare"
    BCRYPT_GENERATE = "crypto::bcrypt::generate"
    BCRYPT_COMPARE = "crypto::bcrypt::compare"
    SHA256 = "crypto::sha256"
    SHA512 = "crypto::sha512"
    MD5 = "crypto::md5"


class SurrealRandFunction(SurrealFunction):
    """``rand::*`` functions (https://surrealdb.com/docs/surrealql/functions/database/rand).

    ``RAND`` is the bare ``rand()`` function; the rest live under the ``rand::`` namespace.
    """

    RAND = "rand"
    UUID = "rand::uuid"
    UUID_V4 = "rand::uuid::v4"
    UUID_V7 = "rand::uuid::v7"
    ULID = "rand::ulid"
    BOOL = "rand::bool"
    FLOAT = "rand::float"
    INT = "rand::int"
    STRING = "rand::string"
    TIME = "rand::time"
    ENUM = "rand::enum"
