# src/surreal_orm_lite/transaction.py
"""ORM-level transactions for the official SurrealDB SDK 2.0.

Two strategies share one public interface:

- ``BufferedTransaction`` (HTTP, and WebSocket on SurrealDB 2.6.x): buffers each
  operation's SurrealQL statement and flushes them as a single ``BEGIN … COMMIT`` query
  at commit. Reads inside the transaction are impossible and raise. This is the v0.8.0
  model.
- ``InteractiveTransaction`` (WebSocket on SurrealDB 3.x): uses the SDK's NATIVE
  transaction API (``client.begin() -> txn_id``, then ``txn_id=`` on every operation,
  then ``client.commit(txn_id)`` / ``client.cancel(txn_id)``). Reads see uncommitted
  writes; the ``txn_id`` isolates the transaction on the shared connection.

``connection_manager.transaction()`` probes ``client.begin()`` to pick the strategy.

A failed statement fails the transaction on both strategies (issue #268). A buffered batch fails as a whole at
commit. The interactive strategy records every statement that fails (a write or a read whose error the ORM does not
answer itself, e.g. a missing table read as no rows); ``commit()`` then cancels instead of committing and raises,
so leaving a ``transaction()`` block after a failed statement raises and nothing of the block is committed, even
when the block caught the statement's error.

Query cache (v0.28.0): an ORM write that joins a transaction notes the tables it writes on it
(``_note_cache_write``); the commit puts them in flight and invalidates their cached reads when
it returns or raises (a failed commit's state is unknown). ``cancel()`` forgets them: nothing
became visible.

Errors (issue #278): the errors a failed transaction raises give the server's reason and the failed statement's
position, without the server's excerpt of the statement; the server's own text stays reachable as their suppressed
``__context__``. A buffered commit fails with ``SurrealDbError`` on every engine (the embedded engine's bare
``RuntimeError`` included), ``SurrealDbConflictError`` for a retryable conflict. After a failed statement, an
interactive transaction's cancellation error names only that statement's error type.
"""

import contextlib
import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, NoReturn

from . import _sdk
from . import cache as _cache
from . import encoding as _encoding
from .exceptions import SurrealDbConflictError, SurrealDbError

#: A ``$name`` parameter of a statement, its name in group 1, matched whole (``BufferedTransaction.add``).
_PARAMETER = re.compile(r"\$(\w+)")

_NOT_EXECUTED_FILLER = "The query was not executed due to a failed transaction"

#: The error ``InteractiveTransaction.commit()`` raises after a failed statement (issue #268), followed by the type
#: name of that statement's error in parentheses (issue #278), e.g. ``(SurrealDbError)``.
_CANCELLED_AFTER_FAILURE = "The transaction was cancelled: a statement in it failed; nothing was committed"

#: Where a server's error starts quoting the query it refused (issue #278): a parse error's `` --> [line:column]``, on
#: a line of its own, followed by an excerpt of the query.
_QUERY_EXCERPT = re.compile(r"\n\s*--> \[(\d+):(\d+)\]")

#: Maps a position of a buffered batch's query (line, column) to the queued statement it falls in (issue #278):
#: ``(number, count, line, column)``, the line and column in the text the caller queued; ``None`` outside every
#: statement (:meth:`BufferedTransaction._locate`).
_Locate = Callable[[int, int], tuple[int, int, int, int] | None]


def _is_pure_filler(result: Any) -> bool:
    """True if a statement's message is ONLY the generic 'not executed' filler.

    On a failed transaction SurrealDB tags every skipped statement with this
    message; the real cause is a different statement. A 2.6.x conflict, by
    contrast, appends the cause to the same string, so it is NOT pure filler.
    """
    return str(result or "").strip().rstrip(".") == _NOT_EXECUTED_FILLER


def _as_conflict(exc: Exception) -> Exception:
    """Return a SurrealDbConflictError if exc is a retryable conflict, else exc itself."""
    from .concurrency import is_conflict_error

    if is_conflict_error(exc):
        return SurrealDbConflictError(str(exc))
    return exc


def _error_type(server_text: str) -> type[SurrealDbError]:
    """``SurrealDbConflictError`` when *server_text* reports a retryable conflict, else ``SurrealDbError``."""
    from .concurrency import is_conflict_error

    return SurrealDbConflictError if is_conflict_error(SurrealDbError(server_text)) else SurrealDbError


def _server_reason(message: str) -> tuple[str, list[tuple[int, int]]]:
    """The server's reason in *message*, without any of the query's text, and the positions it gives (issue #278).

    A message that quotes the query — a parse error, from `` --> [line:column]`` on, and on some errors in the lines
    before it too (a regex's) — is cut to its first line, and every line and column it gives is returned, in order
    (an unclosed delimiter gives where the parser stopped, then where the delimiter opens). Any other message is the
    reason as it is, with no position.
    """
    positions = [(int(found[1]), int(found[2])) for found in _QUERY_EXCERPT.finditer(message)]
    if not positions:
        return message, []
    return message.partition("\n")[0].rstrip(), positions


def _refusal(message: str, locate: _Locate | None = None) -> SurrealDbError:
    """The ORM's error for a query the server refused as a whole with *message* (issue #278).

    ``Transaction failed: the server refused <what>: <reason> (at line <l>, column <c> of <where>)``: the reason is
    :func:`_server_reason`'s, never the query's text. With *locate* (a buffered batch), the position names the queued
    statement, ``statement 2 of 3``, with the line and column in the text the caller queued; without it, the position
    is the statement's (an interactive transaction runs one at a time). Of the positions the server gives, the first
    that falls in a queued statement is used (a statement left unclosed is reported on the ``COMMIT`` line first),
    else the first. ``SurrealDbConflictError`` when the reason reports a retryable conflict: the excerpt, which may
    quote conflict words, is not read.
    """
    reason, positions = _server_reason(message)
    error_type = _error_type(reason)
    if not positions:
        return error_type(f"Transaction failed: the server refused the query: {reason}")
    line, column = positions[0]
    what, where = "the statement", "the statement"
    if locate is not None:
        found = next((place for place in (locate(*position) for position in positions) if place is not None), None)
        if found is None:
            what, where = "the query", "the query"
        else:
            number, count, line, column = found
            what = f"statement {number} of {count}"
    at = f"at line {line}, column {column} of {where}"
    return error_type(f"Transaction failed: the server refused {what}: {reason} ({at})")


def _raise_hiding(error: SurrealDbError, server_text: str) -> NoReturn:
    """Raise *error* ``from None``, *server_text* kept as its ``__context__`` (a ``SurrealDbError``) (issue #278).

    A formatted traceback shows *error* alone: the server's text, which may quote the statement, stays reachable for
    debugging without being printed.
    """
    try:
        raise SurrealDbError(server_text)
    except SurrealDbError:
        raise error from None


def _raise_for_status(raw: Any, locate: _Locate | None = None) -> None:
    """:meth:`Transaction.raise_for_status`, with *locate* for a buffered batch (issue #278): a position then names
    the queued statement (``statement 2 of 3``)."""
    if not isinstance(raw, dict) or not isinstance(raw.get("result"), list):
        envelope = raw.get("error") if isinstance(raw, dict) else None
        message = envelope.get("message") if isinstance(envelope, dict) else None
        if isinstance(message, str) and message:
            _raise_hiding(_refusal(message, locate), message)
        _raise_hiding(
            SurrealDbError(f"Transaction failed: unrecognized query_raw() response shape ({type(raw).__name__})"),
            repr(raw),
        )
    statements = raw["result"]
    errors = [s for s in statements if isinstance(s, dict) and s.get("status") == "ERR"]
    if not errors:
        return
    # Prefer a real error (kind != NotExecuted, and not the generic "not executed"
    # filler); else the first ERR whose message is not that filler (the real cause is
    # sometimes a NotExecuted statement, e.g. "Cannot COMMIT: Transaction conflict ...",
    # and some servers omit `details` on filler statements); else the first ERR.
    root = (
        next(
            (
                e
                for e in errors
                if (e.get("details") or {}).get("kind") != "NotExecuted" and not _is_pure_filler(e.get("result"))
            ),
            None,
        )
        or next((e for e in errors if not _is_pure_filler(e.get("result"))), None)
        or errors[0]
    )
    message = str(root.get("result") or "transaction failed")
    reason, positions = _server_reason(message)
    # Type retryable conflicts so callers / retry_on_conflict can catch them, from the reason alone (#278): a real
    # conflict has no excerpt, and an excerpt may quote conflict words.
    error = _error_type(reason)(f"Transaction failed and rolled back: {reason}")
    if not positions:
        raise error
    _raise_hiding(error, message)


class Transaction(ABC):
    """Base transaction: shared post-commit machinery + the call-site interface.

    Call sites use ``await tx.add(sql, vars)`` for writes and
    ``await tx.run_read(sql, vars)`` for reads, and never care which concrete strategy
    is in use. ``is_interactive`` lets the few read-dependent paths (auto-id save, merge
    refresh, refresh) adapt.
    """

    #: The name of the connection the transaction was opened on (v0.29.0), set by
    #: ``SurrealDBConnectionManager.transaction()``; ``None`` on a transaction built by hand. A model of another
    #: connection refuses to join it (``routing.check_transaction()``, which reads it statically).
    _connection_name: str | None = None
    #: The settings generation of the connection the transaction was opened on (v0.29.0), set by
    #: ``SurrealDBConnectionManager.transaction()``: a connection removed or registered again with other
    #: settings while the block is open is cancelled at its exit instead of committing.
    _connection_generation: int | None = None

    def __init__(self) -> None:
        self._post_commit: list[Callable[[], Awaitable[None]]] = []
        # Query cache: the tables written in this transaction, invalidated at commit.
        self._cache_tables: set[str] = set()
        self._cache_all = False
        # Issue #268: the errors of the statements that failed in this transaction (interactive strategy).
        self._failures: list[BaseException] = []

    def _note_failure(self, error: BaseException) -> None:
        """Record that a statement run in this transaction failed with *error*: the transaction can no longer commit."""
        self._failures.append(error)

    def _absorb_failure(self, error: BaseException) -> None:
        """Forget the failure *error*, which the ORM answered itself (issue #268).

        A read the ORM reads as an answer — a missing table read as no rows, ``get()`` raising "not found", the
        missing-table check of a subquery — does not fail the transaction. Matched by identity; an error that was
        not recorded is ignored.
        """
        self._failures = [failure for failure in self._failures if failure is not error]

    def _first_failure(self) -> BaseException | None:
        """The error of the first statement that failed in this transaction and was not absorbed, else ``None``."""
        return self._failures[0] if self._failures else None

    def _note_cache_write(self, tables: Iterable[str] | None) -> None:
        """Note the tables an ORM write in this transaction changes (``None``: every table).

        ``cache.writing()`` calls it instead of invalidating at once: the rows only become
        visible at commit. ``cache.EDGES`` is noted like a table (a delete in the transaction
        invalidates the edge reads at commit).
        """
        if tables is None:
            self._cache_all = True
        else:
            self._cache_tables |= set(tables)

    def _cache_written(self) -> frozenset[str] | None:
        """The tables noted so far: ``None`` when a write noted every table."""
        return None if self._cache_all else frozenset(self._cache_tables)

    def _forget_cache_writes(self) -> None:
        """Clear the tables noted (after a commit, or on cancel without invalidating)."""
        self._cache_tables.clear()
        self._cache_all = False

    @property
    @abstractmethod
    def is_interactive(self) -> bool:
        """True for the native interactive strategy (reads supported)."""
        ...

    @abstractmethod
    async def add(self, statement: str, variables: dict[str, Any] | None = None) -> Any:
        """Register a write. Buffered: queue (returns None). Interactive: run now
        (returns the statement's result rows)."""
        ...

    @abstractmethod
    async def run_read(self, statement: str, variables: dict[str, Any] | None = None) -> Any:
        """Run a read and return rows. Buffered: raises (reads need WS + SurrealDB 3.x)."""
        ...

    @abstractmethod
    async def commit(self) -> None: ...

    @abstractmethod
    async def cancel(self) -> None: ...

    def enqueue_post_commit(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Enqueue an async callback to run after a successful commit (deferred signals)."""
        self._post_commit.append(callback)

    async def fire_post_commit(self) -> None:
        """Invoke every enqueued post-commit callback in insertion order (FIFO).

        The first callback that raises propagates; the rest are NOT invoked. A raised
        exception does NOT undo the commit (the write is already durable).
        """
        for cb in self._post_commit:
            await cb()

    @staticmethod
    def raise_for_status(raw: Any) -> None:
        """Inspect a ``query_raw()`` response and raise if the query or any statement failed.

        ``query()`` silently returns ``None`` on a failed transaction, so the
        commit/write paths use ``query_raw()`` and call this. Any statement with
        ``status == "ERR"`` means the transaction is rolled back: a buffered batch at once, an
        interactive transaction when it ends (its ``commit()`` cancels after a failed statement,
        issue #268). Prefers the root cause (the first ERR whose ``details.kind`` is not
        ``"NotExecuted"``).

        The error leaves out the server's excerpt of the query (issue #278). A query the server
        refused as a whole (an ``error`` envelope, e.g. a statement it cannot parse) raises
        ``Transaction failed: the server refused …`` with the server's reason and the position it
        gives (:func:`_refusal`); a failed statement raises ``Transaction failed and rolled back:
        <the server's reason>``, a reason that quotes the query cut to its first line. Whenever the
        message leaves part of the server's text out, that text is kept as the error's suppressed
        ``__context__``. ``SurrealDbConflictError`` for a retryable conflict.
        """
        _raise_for_status(raw)


class BufferedTransaction(Transaction):
    """HTTP / WS-2.6.x strategy (v0.8.0): buffer statements, flush as one BEGIN…COMMIT.

    Requires a shared client at commit time, injected by the connection manager. Record
    ids inlined into statements are the caller's responsibility (see save()).
    """

    def __init__(self, client: Any = None) -> None:
        super().__init__()
        self._client = client
        self.statements: list[str] = []
        self.variables: dict[str, Any] = {}
        self._counter: int = 0
        # Issue #278: per queued statement, the length of its prefix and where add() inserted it (offsets in the
        # caller's text), so that a position the server gives is moved back to the caller's column.
        self._renamed_at: list[tuple[int, list[int]]] = []

    @property
    def is_interactive(self) -> bool:
        return False

    @property
    def is_empty(self) -> bool:
        return not self.statements

    async def add(self, statement: str, variables: dict[str, Any] | None = None) -> None:
        """Append a statement, renaming its ``$vars`` with a per-statement prefix.

        Each ``$name`` of ``variables`` becomes ``$t<N>_name``, in a single pass over the statement (#261): a name is
        matched whole, so one that is a prefix of another is not corrupted, a renamed name is never renamed again, and
        a parameter that is not one of ``variables`` (``$this``, ``$value``…) is left alone. Returns None: nothing
        executes until commit, so buffered writes cannot return rows. Where each prefix goes is noted, so that a
        position the server reports at commit is given in the caller's text (issue #278).
        """
        variables = _encoding.encode(variables)
        prefix = f"t{self._counter}_"
        renamed = statement
        inserted: list[int] = []
        if variables:
            names = set(variables)

            def rename(match: re.Match[str]) -> str:
                if match[1] not in names:
                    return match[0]
                inserted.append(match.start() + 1)  # right after the "$"
                return f"${prefix}{match[1]}"

            renamed = _PARAMETER.sub(rename, statement)
            for name, value in variables.items():
                self.variables[f"{prefix}{name}"] = value
        self.statements.append(renamed)
        self._renamed_at.append((len(prefix), inserted))
        self._counter += 1
        return None

    async def run_read(self, statement: str, variables: dict[str, Any] | None = None) -> Any:
        raise SurrealDbError(
            "Reads inside a transaction require a WebSocket connection to SurrealDB 3.x "
            "(native interactive transactions); the current transaction is buffered "
            "(HTTP or SurrealDB 2.6.x)."
        )

    def build_query(self) -> str:
        """Return the batched ``BEGIN … COMMIT`` query string."""
        if self.is_empty:
            raise ValueError("Cannot commit an empty transaction.")
        body = "\n".join(self.statements)
        return f"BEGIN TRANSACTION;\n{body}\nCOMMIT TRANSACTION;"

    def _locate(self, line: int, column: int) -> tuple[int, int, int, int] | None:
        """The queued statement that *line* of :meth:`build_query`'s query falls in, and the position in the caller's
        text: ``(number, count, line, column)``; ``None`` outside every statement (issue #278).

        Line 1 is ``BEGIN TRANSACTION;``; each statement starts on the line after the previous one ends.
        """
        first = 2
        for index, statement in enumerate(self.statements):
            last = first + statement.count("\n")
            if first <= line <= last:
                own_line = line - first + 1
                return index + 1, len(self.statements), own_line, self._caller_column(index, own_line, column)
            first = last + 1
        return None

    def _caller_column(self, index: int, line: int, column: int) -> int:
        """*column* of *line* of the queued statement *index*, moved back over the prefixes :meth:`add` inserted
        before it on that line. SurrealDB counts columns in characters, as Python does; a column inside a prefix
        becomes the name's first character. Unchanged when ``statements`` no longer matches what :meth:`add` noted.
        """
        if len(self._renamed_at) != len(self.statements):
            return column
        size, inserted = self._renamed_at[index]
        line_start = sum(len(text) + 1 for text in self.statements[index].split("\n")[: line - 1])
        offset = line_start + column - 1
        on_earlier_lines = before = 0
        for earlier, original in enumerate(inserted):
            start = original + earlier * size  # where this prefix starts in the queued text
            if start + size <= line_start:
                on_earlier_lines += 1
            if start + size <= offset:
                before += 1
            elif start <= offset:
                return start - line_start + 1 - (before - on_earlier_lines) * size
        return column - (before - on_earlier_lines) * size

    async def commit(self) -> None:
        # The tables noted are in flight during the commit and invalidated when it returns
        # or raises (a failed commit's state is unknown).
        try:
            async with _cache.writing(self._cache_written()):
                if self.is_empty:
                    return
                # query_raw (NOT query): query() returns None on a failed tx; query_raw gives
                # per-statement status for raise_for_status.
                try:
                    raw = await self._client.query_raw(self.build_query(), _encoding.encode(self.variables))
                except RuntimeError as exc:
                    # The embedded engine raises a bare RuntimeError for a query it refuses as a whole (issue
                    # #278): the same error as a server's envelope, the engine's error kept as the suppressed
                    # __context__. A server client's RuntimeError is not the server's answer: it propagates.
                    embedded = _sdk.SdkAsyncEmbeddedConnection
                    if embedded is None or not isinstance(self._client, embedded):
                        raise
                    raise _refusal(str(exc), self._locate) from None
                _raise_for_status(raw, self._locate)
        finally:
            self._forget_cache_writes()

    async def cancel(self) -> None:
        # Nothing was sent (statements buffered) → rollback is free; nothing became visible,
        # so the tables noted are forgotten, not invalidated.
        self._forget_cache_writes()
        self.statements = []
        self.variables = {}
        self._renamed_at = []


class InteractiveTransaction(Transaction):
    """WebSocket / SurrealDB 3.x strategy using the SDK's native transaction API.

    Built with ``(client, txn_id)`` where ``txn_id`` comes from ``client.begin()``
    (done by the connection manager). Every operation passes ``txn_id=`` so it joins the
    server-side transaction; reads see uncommitted writes and are isolated from other
    operations on the same connection.

    A statement that fails — its request raises, or the server reports an error — is recorded
    (issue #268); its error still raises at once. ``commit()`` then cancels the transaction
    instead of committing it and raises :data:`_CANCELLED_AFTER_FAILURE`, so the transaction fails
    as a whole, as a buffered one does, even when the caller caught the statement's error.
    """

    def __init__(self, client: Any, txn_id: Any) -> None:
        super().__init__()
        self._client = client
        self._txn_id = txn_id
        self._cancelled = False

    @property
    def is_interactive(self) -> bool:
        return True

    async def add(self, statement: str, variables: dict[str, Any] | None = None) -> Any:
        """Run a write inside the transaction; inspect status; return its rows.

        A failure is recorded before it raises: the transaction will not commit (issue #268).
        """
        payload = _encoding.encode(variables or {})
        try:
            try:
                raw = await self._client.query_raw(statement, payload, txn_id=self._txn_id)
            except Exception as exc:
                raise _as_conflict(exc) from exc
            self.raise_for_status(raw)
        except BaseException as failure:
            self._note_failure(failure)
            raise
        statements = raw["result"]
        return statements[-1]["result"] if statements else []

    async def run_read(self, statement: str, variables: dict[str, Any] | None = None) -> Any:
        """Run a read inside the transaction and return rows directly.

        A failure is recorded before it raises (issue #268); the ORM forgets it when it reads the
        error as an answer (:meth:`_absorb_failure`).
        """
        payload = _encoding.encode(variables or {})
        try:
            try:
                return await self._client.query(statement, payload, txn_id=self._txn_id)
            except Exception as exc:
                raise _as_conflict(exc) from exc
        except BaseException as failure:
            self._note_failure(failure)
            raise

    async def commit(self) -> None:
        """Commit the transaction; after a failed statement, cancel it and raise instead (issue #268).

        Raises:
            SurrealDbError: a statement failed in this transaction (:data:`_CANCELLED_AFTER_FAILURE`, followed by the
                type name of that statement's error, e.g. ``(SurrealDbError)``; the error itself is chained as the
                cause): the transaction was cancelled, nothing was committed. ``SurrealDbConflictError`` when that
                statement's error was a retryable conflict.
        """
        failure = self._first_failure()
        if failure is not None:
            from .concurrency import is_conflict_error

            await self.cancel()
            error_type = SurrealDbConflictError if is_conflict_error(failure) else SurrealDbError
            # Only the type name (issue #278): the failed statement's error is the cause, its text is not repeated.
            raise error_type(f"{_CANCELLED_AFTER_FAILURE} ({type(failure).__name__})") from failure
        # The tables noted are in flight during the commit and invalidated when it returns
        # or raises (a failed commit's state is unknown).
        try:
            async with _cache.writing(self._cache_written()):
                try:
                    await self._client.commit(self._txn_id)
                except Exception as exc:
                    raise _as_conflict(exc) from exc
        finally:
            self._forget_cache_writes()

    async def cancel(self) -> None:
        # Nothing became visible: the tables noted are forgotten, not invalidated. Sent once: a
        # commit() that cancelled after a failed statement is cancelled again by transaction().
        self._forget_cache_writes()
        if self._cancelled:
            return
        self._cancelled = True
        with contextlib.suppress(Exception):
            await self._client.cancel(self._txn_id)
