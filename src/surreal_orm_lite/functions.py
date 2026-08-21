"""Server-side computation helpers (v0.13.0, v0.14.0).

``SurrealFunc`` marks a value as a **raw SurrealQL expression** to be evaluated by the
server instead of being sent as data. It is the building block of ``server_values=`` on
:meth:`BaseSurrealModel.save` / :meth:`BaseSurrealModel.merge`.

``Computed[T]`` + ``computed("expr")`` (v0.14.0) attach such an expression to the **schema**
instead of to a single write: the field is declared with ``DEFINE FIELD … VALUE`` and recomputed
by the server on every write to the table. It reuses ``SurrealFunc`` for expression validation.

This module deliberately imports nothing from the rest of the package (``utils`` imports
*it*), so it stays cycle-free.
"""

import re
from enum import StrEnum
from typing import Annotated, Any, Optional, TypeAlias, TypeVar

# A SurrealQL variable name: the ``$`` is the sigil, the name itself is an identifier.
VALID_VARIABLE_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

__all__ = [
    "Computed",
    "Var",
    "SurrealArrayFunction",
    "SurrealCryptoFunction",
    "SurrealFunc",
    "SurrealFunction",
    "SurrealMathFunction",
    "SurrealRandFunction",
    "SurrealStringFunction",
    "SurrealTimeFunction",
    "computed",
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
    """Annotation marker identifying a computed field.

    Lives inside ``Annotated[T | None, _COMPUTED_MARKER]``. Pydantic ignores metadata it does
    not recognise, so this is inert at validation time — it exists so tooling (and a reader)
    can tell a computed field from an ordinary optional one.

    A single shared instance (``_COMPUTED_MARKER``) rather than one per subscript: ``Computed``
    is a *generic alias*, so the metadata is fixed when the alias is defined and cannot carry
    the per-use inner type. The inner type is already recoverable from the annotation itself.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "_ComputedMarker()"


_COMPUTED_MARKER = _ComputedMarker()

_T = TypeVar("_T")

Computed: TypeAlias = Annotated[Optional[_T], _COMPUTED_MARKER]  # noqa: UP045
"""Annotation for a field SurrealDB computes on **every write**, via ``DEFINE FIELD … VALUE``.

Pair it with :func:`computed`, which carries the expression::

    from surreal_orm_lite import BaseSurrealModel, Computed, computed

    class Player(BaseSurrealModel):
        id: str
        first_name: str
        last_name: str
        full_name: Computed[str] = computed("string::concat(first_name, ' ', last_name)")

    await Player.define_computed_fields()   # DEFINE FIELD OVERWRITE … VALUE …

``Computed[T]`` expands to ``Annotated[T | None, _COMPUTED_MARKER]``: the field is nullable and
defaults to ``None``, so an instance is constructible before the server has ever computed it.

It is a plain generic type alias rather than a class with ``__class_getitem__`` so that mypy and
pyright resolve ``player.full_name`` to ``T | None`` in user code — the split between annotation
and factory mirrors SQLAlchemy's ``Mapped[T] = mapped_column(...)``.

``Optional[...]`` rather than ``... | None`` so a forward reference (``Computed["Order"]``)
works too — ``"Order" | None`` would raise ``TypeError``. The two spellings are equal for real
types.
"""


class _ComputedDefault:
    """Sentinel default produced by ``computed("expr")``.

    ``BaseSurrealModel.__init_subclass__`` collects these from the class body and replaces
    each with ``None`` before Pydantic processes the fields, so the model ends up with a
    plain nullable field plus a recorded expression.
    """

    __slots__ = ("expression",)

    def __init__(self, expression: str) -> None:
        self.expression = expression

    def __repr__(self) -> str:
        return f"computed({self.expression!r})"


def computed(expression: "str | SurrealFunc") -> Any:
    """Declare the SurrealQL expression behind a :data:`Computed` field.

    The default half of a computed field; the annotation half is ``Computed[T]``, and both
    belong on the same line::

        class Player(BaseSurrealModel):
            id: str
            first_name: str
            last_name: str
            full_name: Computed[str] = computed("string::concat(first_name, ' ', last_name)")

        await Player.define_computed_fields()   # DEFINE FIELD OVERWRITE … VALUE …

    The ORM then excludes ``full_name`` from every write payload and hydrates it from the row
    the server returns. Naming it in ``merge()``, ``patch()``, ``bulk_update()`` or an
    ``atomic_*`` helper raises ``ValueError`` — the server would discard the write anyway.

    Where :class:`SurrealFunc` evaluates an expression for **one** write, a computed field
    attaches it to the **schema**: it applies to every write on the table, including ones the
    ORM never sees. That makes it a server-enforced invariant rather than a convention.

    A subclass may redeclare an inherited computed field with a new expression, or demote it to
    an ordinary writable field by redeclaring it without ``computed(...)``.

    Returns ``Any`` deliberately: the runtime value is an internal sentinel that
    ``__init_subclass__`` swaps for ``None``, so declaring it as such would make every
    ``Computed[T] = computed(...)`` line a type error in user code.

    Args:
        expression: a SurrealQL expression, as a plain string or a :class:`SurrealFunc`.
            Validated by :class:`SurrealFunc` either way.

    Note:
        SurrealDB evaluates computed fields in **alphabetical field-name order**, not
        declaration order. A computed field reading another must sort after it: ``subtotal``
        → ``total`` resolves, but ``z_sub`` → ``a_total`` fails at write time.

    Note:
        The annotation is mandatory — a bare ``full_name = computed("…")`` is rejected by
        Pydantic itself, before the ORM ever sees it.

    Warning:
        The expression is inlined **verbatim** into DDL and cannot reference bound parameters.
        Build it only from developer-controlled text, never from user input — the same trust
        model as :class:`SurrealFunc`, whose validation it reuses.
    """
    func = expression if isinstance(expression, SurrealFunc) else SurrealFunc(expression)
    return _ComputedDefault(func.expression)


class Var:
    """An explicit reference to a **bound query variable** in a filter (v0.14.3).

    ``filter()`` has always read a string starting with ``$`` as a variable reference, which
    makes a literal value like ``"$admin"`` impossible to express and turns any user-supplied
    string into an accidental reference. ``Var`` states the intent instead of inferring it::

        from surreal_orm_lite import Var

        qs = User.objects().filter(age__gte=Var("min_age"))     # … WHERE age >= $min_age

    The variable itself is supplied at execution time, exactly as before. Plain strings
    beginning with ``$`` still work but are deprecated; write ``"$$admin"`` to filter on a
    literal that starts with a dollar sign.
    """

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        candidate = name[1:] if name.startswith("$") else name
        if not VALID_VARIABLE_NAME.match(candidate):
            raise ValueError(
                f"Invalid variable name '{name}': a variable name must start with a letter or "
                "underscore and contain only letters, digits and underscores"
            )
        self.name = candidate

    @property
    def reference(self) -> str:
        """The SurrealQL form of the reference, e.g. ``$min_age``."""
        return f"${self.name}"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Var({self.name!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Var) and other.name == self.name

    def __hash__(self) -> int:
        return hash((Var, self.name))


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
