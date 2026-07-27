"""Server-side computation helpers (v0.13.0, v0.14.0).

``SurrealFunc`` marks a value as a **raw SurrealQL expression** to be evaluated by the
server instead of being sent as data. It is the building block of ``server_values=`` on
:meth:`BaseSurrealModel.save` / :meth:`BaseSurrealModel.merge`.

``Computed`` (v0.14.0) attaches such an expression to the **schema** instead of to a single
write: the field is declared with ``DEFINE FIELD … VALUE`` and recomputed by the server on
every write to the table. It reuses ``SurrealFunc`` for expression validation.

This module deliberately imports nothing from the rest of the package (``utils`` imports
*it*), so it stays cycle-free.
"""

from enum import StrEnum
from typing import Annotated, Any, Optional

__all__ = [
    "Computed",
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
    def call(cls, fn: "str | SurrealFunction", *args: str) -> "SurrealFunc":
        """Build ``fn(arg, ...)`` from a function name and raw SurrealQL argument fragments.

        ``fn`` is a plain string or any :class:`SurrealFunction` member (its value is the
        function name)::

            SurrealFunc.call(SurrealTimeFunction.NOW)                      # time::now()
            SurrealFunc.call(SurrealCryptoFunction.ARGON2_GENERATE, "$pw")  # crypto::argon2::generate($pw)

        Each argument is a **raw SurrealQL fragment**, not a value: a bound-parameter
        reference like ``"$password"``, a field name, or a literal written as it appears in
        the query (``"'a'"`` for a string, ``"1w"`` for a duration). Passing a bare Python
        value would render it unquoted — route values through ``extra_vars`` and reference
        them by name instead. Arguments carry the same developer-controlled trust model as
        the expression itself.
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


class _ComputedMarker:
    """Annotation marker identifying a computed field and carrying its inner type.

    Lives inside ``Annotated[T | None, _ComputedMarker(T)]``. Pydantic ignores metadata it
    does not recognise, so this is inert at validation time — it exists so tooling (and a
    reader) can tell a computed field from an ordinary optional one.
    """

    __slots__ = ("inner_type",)

    def __init__(self, inner_type: Any) -> None:
        self.inner_type = inner_type

    def __repr__(self) -> str:
        return f"_ComputedMarker({self.inner_type!r})"


class _ComputedDefault:
    """Sentinel default produced by ``Computed("expr")``.

    ``BaseSurrealModel.__init_subclass__`` collects these from the class body and replaces
    each with ``None`` before Pydantic processes the fields, so the model ends up with a
    plain nullable field plus a recorded expression.
    """

    __slots__ = ("expression",)

    def __init__(self, expression: str) -> None:
        self.expression = expression

    def __repr__(self) -> str:
        return f"Computed({self.expression!r})"


class Computed:
    """A field whose value SurrealDB computes on **every write**, via ``DEFINE FIELD … VALUE``.

    Dual-use, and both halves belong on the same line:

    - ``Computed[T]`` — the annotation, resolving to ``Annotated[T | None, _ComputedMarker(T)]``.
      The field is nullable and defaults to ``None`` so an instance is constructible before the
      server has ever computed it.
    - ``Computed("expr")`` — the default, carrying the SurrealQL expression::

        class Player(BaseSurrealModel):
            id: str
            first_name: str
            last_name: str
            full_name: Computed[str] = Computed("string::concat(first_name, ' ', last_name)")

        await Player.define_computed_fields()   # DEFINE FIELD OVERWRITE … VALUE …

    The ORM then excludes ``full_name`` from every write payload and hydrates it from the row
    the server returns. Naming it in ``merge()``, ``bulk_update()`` or an ``atomic_*`` helper
    raises ``ValueError`` — the server would discard the write anyway.

    Where :class:`SurrealFunc` evaluates an expression for **one** write, ``Computed`` attaches
    it to the **schema**: it applies to every write on the table, including ones the ORM never
    sees. That makes it a server-enforced invariant rather than a convention.

    A subclass may redeclare an inherited computed field with a new expression, or demote it to
    an ordinary writable field by redeclaring it without ``Computed(...)``.

    Note:
        SurrealDB evaluates computed fields in **alphabetical field-name order**, not
        declaration order. A computed field reading another must sort after it: ``subtotal``
        → ``total`` resolves, but ``z_sub`` → ``a_total`` fails at write time.

    Note:
        The annotation is mandatory — a bare ``full_name = Computed("…")`` is rejected by
        Pydantic itself, before the ORM ever sees it.

    Warning:
        The expression is inlined **verbatim** into DDL and cannot reference bound parameters.
        Build it only from developer-controlled text, never from user input — the same trust
        model as :class:`SurrealFunc`, whose validation it reuses.
    """

    __slots__ = ()

    def __class_getitem__(cls, inner_type: Any) -> Any:
        """``Computed[str]`` → ``Annotated[str | None, _ComputedMarker(str)]``.

        ``Optional[...]`` rather than ``... | None`` so a forward reference
        (``Computed["Order"]``) works too — ``"Order" | None`` would raise ``TypeError``.
        The two spellings are equal for real types.
        """
        return Annotated[Optional[inner_type], _ComputedMarker(inner_type)]  # noqa: UP045

    def __new__(cls, expression: "str | SurrealFunc") -> "_ComputedDefault":  # type: ignore[misc]
        """``Computed("expr")`` → ``_ComputedDefault("expr")``, validated via ``SurrealFunc``."""
        func = expression if isinstance(expression, SurrealFunc) else SurrealFunc(expression)
        return _ComputedDefault(func.expression)


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
