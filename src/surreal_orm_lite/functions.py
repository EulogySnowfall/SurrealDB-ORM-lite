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
from collections.abc import Sequence
from enum import StrEnum
from typing import Annotated, Any, Optional, TypeAlias, TypeVar

# A SurrealQL variable name: the ``$`` is the sigil, the name itself is an identifier.
VALID_VARIABLE_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

__all__ = [
    "CALL_ARG_PREFIX",
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
    "build_call_statement",
    "computed",
    "normalize_function_name",
    "parse_function_parameters",
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


# ---------------------------------------------------------------------------
# Stored function calls (v0.15.0)
#
# Pure helpers behind ``SurrealDBConnectionManager.call_function()``. They do no I/O, so the
# call path can be reasoned about (and unit-tested) without a server.
# ---------------------------------------------------------------------------

#: One ``::``-separated segment of a stored-function name.
_FUNCTION_SEGMENT = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")

#: The parameter prefix used when binding a stored function's arguments.
CALL_ARG_PREFIX = "_fnarg"

_DEFINE_FUNCTION_HEAD = re.compile(r"\bDEFINE\s+FUNCTION\b", re.IGNORECASE)

#: A parameter reference in a ``DEFINE FUNCTION`` signature, in any of the three forms the
#: server may echo back. SurrealDB **quotes a name that collides with a reserved word**, and
#: the two DB lines disagree about when: 2.6.x renders ``fn::f($by: int)`` as ``$`by```, while
#: 3.x leaves it bare. Missing the quoted form silently yields an EMPTY signature, which is
#: how this was found — ``params={"by": …}`` failed only on 2.6.5.
_PARAMETER_REFERENCE = re.compile(
    r"\$(?:([a-zA-Z_][a-zA-Z0-9_]*)|`([^`]+)`|⟨([^⟩]+)⟩)",
)


def normalize_function_name(function: str) -> str:
    """Return *function* as a validated ``fn::…`` name, adding the prefix when absent.

    The name is **interpolated** into the generated statement, because SurrealQL does not
    accept a bound parameter in call position. It is therefore validated first: after the
    ``fn::`` prefix, the name must be one or more identifier segments joined by ``::``
    (``fn::greet``, ``fn::billing::total``). Everything else is rejected::

        normalize_function_name("greet")             # "fn::greet"
        normalize_function_name("fn::billing::sum")  # "fn::billing::sum"
        normalize_function_name("fn::x; DROP TABLE") # ValueError

    Arguments are never interpolated — :func:`build_call_statement` binds them.

    :raises TypeError: if *function* is not a string.
    :raises ValueError: if the name is empty or is not an identifier path.
    """
    if not isinstance(function, str):
        raise TypeError(f"Function name must be a string, got {type(function).__name__!r}.")

    name = function.strip()
    if not name:
        raise ValueError("Function name cannot be empty.")

    if name.startswith("fn::"):
        name = name[len("fn::") :]

    segments = name.split("::")
    # fullmatch, not match: '$' also matches before a trailing newline, so 'ab\n' would pass.
    if not segments or not all(_FUNCTION_SEGMENT.fullmatch(segment) for segment in segments):
        raise ValueError(
            f"Invalid stored function name: {function!r}. Expected 'fn::name' or "
            "'fn::namespace::name', where each segment is an identifier "
            "([A-Za-z_][A-Za-z0-9_]*)."
        )

    return f"fn::{'::'.join(segments)}"


def parse_function_parameters(define_statement: str) -> tuple[str, ...]:
    """Read a function's parameter names, in declaration order, from its ``DEFINE`` text.

    ``INFO FOR DB`` returns each stored function as the full statement that defines it::

        DEFINE FUNCTION fn::greet($name: string, $times: int) { … } PERMISSIONS FULL

    from which this recovers ``("name", "times")`` — the order in which
    :meth:`SurrealDBConnectionManager.call_function` must pass named ``params``, since
    SurrealQL function arguments are positional.

    Only the parameter list is read: the scan tracks bracket depth and collects ``$ident`` at
    depth 1, so a ``TYPE`` clause containing commas or angle brackets
    (``$opts: option<array<string>>``) cannot split it wrongly, and the body is never reached.

    :raises ValueError: if the text is not a ``DEFINE FUNCTION`` statement.
    """
    if not isinstance(define_statement, str) or not _DEFINE_FUNCTION_HEAD.search(define_statement):
        raise ValueError(f"Not a DEFINE FUNCTION statement: {define_statement!r}")

    start = define_statement.find("(")
    if start == -1:
        raise ValueError(f"DEFINE FUNCTION statement has no parameter list: {define_statement!r}")

    names: list[str] = []
    depth = 0
    index = start
    while index < len(define_statement):
        char = define_statement[index]
        if char in "'\"":
            # Skip a string literal wholesale: a literal type such as `$mode: 'a)b' | 'c'` would
            # otherwise close the parameter list early, and one containing a `$` would invent a
            # phantom parameter. Backslash escapes are honoured, so `'a\\'b'` is one literal and
            # the scan does not resume inside it and run past the closing paren.
            index += 1
            while index < len(define_statement):
                if define_statement[index] == "\\":
                    index += 2
                    continue
                if define_statement[index] == char:
                    index += 1
                    break
                index += 1
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                break
        elif char == "$" and depth == 1:
            match = _PARAMETER_REFERENCE.match(define_statement[index:])
            if match:
                # Exactly one of the three alternatives matched: bare, `backtick`, or ⟨bracket⟩.
                names.append(next(group for group in match.groups() if group is not None))
                index += match.end() - 1
        index += 1
    else:  # pragma: no cover - defensive: a truncated statement from the server
        raise ValueError(f"Unterminated parameter list in: {define_statement!r}")

    return tuple(names)


def build_call_statement(function: str, args: Sequence[Any]) -> tuple[str, dict[str, Any]]:
    """Build the statement and bound variables that call *function* with *args*.

        build_call_statement("fn::greet", ["ada", 3])
        # ("fn::greet($_fnarg0, $_fnarg1);", {"_fnarg0": "ada", "_fnarg1": 3})

    The **bare call form** is deliberate: a ``RETURN`` statement inside a
    ``BEGIN … COMMIT`` batch terminates the transaction early — and silently, on both
    SurrealDB 2.6.x and 3.x — so every statement queued after it is reported as successful
    and never runs. The bare form returns the same value, in every context, without that
    hazard. See the v0.15.0 design spec.

    *function* is expected to be already normalised by :func:`normalize_function_name`.
    """
    variables = {f"{CALL_ARG_PREFIX}{index}": value for index, value in enumerate(args)}
    rendered = ", ".join(f"${name}" for name in variables)
    return f"{function}({rendered});", variables
