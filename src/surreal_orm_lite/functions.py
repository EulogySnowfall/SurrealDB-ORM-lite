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
    "SurrealFunc",
    "SurrealFunction",
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
    """
