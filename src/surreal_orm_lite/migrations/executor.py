"""Applying and undoing migrations (the full SurrealDB-ORM's ``migrations.executor``).

:class:`MigrationExecutor` works on a migrations directory: its files (``get_available_migrations()``,
``load_migration()``), their application order (``get_pending_migrations()``), the state they build
(``migration_state()``) and the statements of one of them (``show_sql()``) — all pure: no database is read. It applies
the pending ones (``migrate()``, spec §5.6), undoes the latest ones (``rollback()``), runs the data operations of the
applied ones (``upgrade()``), reports every file (``get_migration_status()``) and records the applied names in the
tracking table ``_surreal_orm_migrations`` (``ensure_migrations_table()``, ``get_applied_migrations()``).

``migrate()`` and ``rollback()`` are anchored on the database's schema (D35): each reads it once, before any DDL —
``migrate()`` replays the planned migrations over it strictly (E-R1), ``rollback()`` rewinds it migration by migration —
and renders every statement from those states before the first batch is sent. Each migration is one ``BEGIN … COMMIT``
batch holding its tracking statement first (phase B: the record's ``CREATE``, or its ``DELETE`` for a rollback); the
analyzer definitions it commits are sent in a batch of their own just before (phase A, M4, M26) and restored to the
definitions the database held when phase B fails (D38). The data operations run after the schema (phase C). A failed
batch is read through the line's status layout (§5.6.1), and the tracking table is read again before anything else is
decided: a migration a concurrent run applied (or rolled back) is skipped, a retryable conflict raises
``SurrealDbConflictError``, a refusal ``MigrationStatementError`` — whose message names the migration, the operation and
the server's reason, never a statement. Logs (logger ``surreal_orm_lite.migrations``) name migrations, operations and
reasons only.
"""

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .. import _sdk
from .. import cache as _cache
from .. import encoding as _encoding
from ..concurrency import is_conflict_error
from ..connection_manager import SurrealDBConnectionManager
from ..exceptions import SurrealDbConflictError, SurrealDbError
from ..introspection import DatabaseIntrospector
from ..routing import check_name
from ..schema_state import SchemaIssue, SchemaState
from ..search import checked_line
from ..time_travel import cached_major
from ..transaction import _is_pure_filler, _server_reason
from . import loader, render
from . import state as _state
from .constants import MIGRATIONS_TABLE
from .migration import Migration, _statements
from .operations import DataMigration, DefineAnalyzer, RemoveAnalyzer

__all__ = ["MigrationExecutor", "MigrationStatementError"]

logger = logging.getLogger("surreal_orm_lite.migrations")

#: The tracking table, its fields and its unique index (the full ORM's, D5), each ``IF NOT EXISTS`` (M8).
_TRACKING_DEFINITIONS = (
    f"DEFINE TABLE IF NOT EXISTS `{MIGRATIONS_TABLE}` SCHEMAFULL;",
    f"DEFINE FIELD IF NOT EXISTS `name` ON `{MIGRATIONS_TABLE}` TYPE string;",
    f"DEFINE FIELD IF NOT EXISTS `applied_at` ON `{MIGRATIONS_TABLE}` TYPE datetime DEFAULT time::now();",
    f"DEFINE INDEX IF NOT EXISTS `migration_name` ON `{MIGRATIONS_TABLE}` FIELDS `name` UNIQUE;",
)
#: The tracking record of phase B, first in its batch: a ``LET`` in a later ``RawSQL`` cannot change the name (M25).
_RECORD = f"CREATE `{MIGRATIONS_TABLE}` SET name = $surreal_orm_migration;"
#: The tracking record's removal, first in a rollback's phase-B batch.
_UNRECORD = f"DELETE `{MIGRATIONS_TABLE}` WHERE name = $surreal_orm_migration;"
#: The applied names, in the order they were applied (the full ORM's query, ties broken by name).
_APPLIED = f"SELECT name, applied_at FROM `{MIGRATIONS_TABLE}` ORDER BY applied_at, name;"
#: 3.x's text for the statements after the failing one (M2): filler, as ``transaction._is_pure_filler()``'s.
_CANCELLED_FILLER = "The query was not executed due to a cancelled transaction"
#: How many times ``ensure_migrations_table()`` sends its batch when a concurrent first run conflicts with it.
_ENSURE_ATTEMPTS = 3
#: The compensation sentence (spec §7), appended after a space.
_RESTORED = "The analyzer definitions it committed first were restored."

#: The refusal of a client object as ``connection=`` (v0.30.0's rule).
_CLIENT_OBJECT = (
    "connection= takes the name of a registered connection, not a client object: pass the name it was added with "
    "(add_connection()), or leave it None and make the call inside SurrealDBConnectionManager.using(name)."
)


def _rebuild_statement_error(
    cls: type["MigrationStatementError"],
    message: str,
    migration: str,
    operation: int | None,
    description: str | None,
    analyzers_restored: bool | None,
) -> "MigrationStatementError":
    """Rebuild a pickled :class:`MigrationStatementError` (its keyword-only arguments included)."""
    return cls(
        message,
        migration=migration,
        operation=operation,
        description=description,
        analyzers_restored=analyzers_restored,
    )


class MigrationStatementError(SurrealDbError, RuntimeError):
    """SurrealDB refused a statement of a migration, or its tracking record.

    Caught by ``except SurrealDbError`` (the lite's errors) and by ``except RuntimeError`` (the full SurrealDB-ORM's
    base for this error). The message names the migration, the operation and the server's reason; it never quotes
    the statement.

    Attributes:
        migration: The migration's name.
        operation: The failing operation's position in the migration (1-based), or ``None`` when no operation is to
            blame (the tracking record, a batch refused as a whole).
        description: That operation's ``describe()`` text, or ``None``.
        analyzers_restored: Whether the analyzer definitions the migration committed before its transaction were
            restored after the failure; ``None`` when it committed none.
    """

    def __init__(
        self,
        message: str,
        *,
        migration: str,
        operation: int | None = None,
        description: str | None = None,
        analyzers_restored: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.migration = migration
        self.operation = operation
        self.description = description
        self.analyzers_restored = analyzers_restored

    def __reduce__(self) -> tuple[Any, ...]:
        """Pickle with the keyword-only arguments (``Exception.__reduce__`` would call ``cls(*args)`` and fail)."""
        arguments = (type(self), self.args[0], self.migration, self.operation, self.description, self.analyzers_restored)
        return _rebuild_statement_error, arguments, self.__dict__


# --- the batches and their answers (spec §5.6.1) ------------------------------------------------------------------


def _terminated(statement: str) -> str:
    """*statement* without trailing blanks, ending with ``;`` (a ``RawSQL`` may be written without it)."""
    text = statement.rstrip()
    return text if text.endswith(";") else f"{text};"


class _Batch:
    """One ``BEGIN … COMMIT`` batch: its statements — each an operation's text, which may hold several statements (an
    ``AlterTable``'s line-2 redefinitions, a ``RawSQL``), on its own lines — and the operation each belongs to (its
    index in the migration's operations; ``None`` for the tracking statement and a compensation's).

    Attributes:
        statements: The texts, each ending with ``;``.
        owners: The operation of each text.
        counts: The statements of each text, as SurrealDB answers them (``render.check_statements()``: an empty
            statement has no entry, M2).
    """

    __slots__ = ("counts", "owners", "statements")

    def __init__(self, statements: Sequence[str], owners: Sequence[int | None]) -> None:
        self.statements = tuple(_terminated(statement) for statement in statements)
        self.owners = tuple(owners)
        self.counts = tuple(render.check_statements(statement, what="Migration") for statement in self.statements)

    @property
    def size(self) -> int:
        """The statements of the batch, as SurrealDB answers them."""
        return sum(self.counts)

    def text(self) -> str:
        """The query sent: ``BEGIN TRANSACTION;``, each text on the lines after it, ``COMMIT TRANSACTION;``."""
        return "\n".join(("BEGIN TRANSACTION;", *self.statements, "COMMIT TRANSACTION;"))

    def statement_of(self, entry: int) -> int:
        """The text the *entry*-th statement answered (0-based, ``BEGIN`` not counted) belongs to."""
        end = 0
        for index, count in enumerate(self.counts):
            end += count
            if entry < end:
                return index
        return len(self.counts) - 1  # pragma: no cover - the caller checked the layout's length

    def locate(self, line: int) -> tuple[int, int] | None:
        """The text *line* of :meth:`text` falls in, and the line in that text (1-based); ``None`` outside every text
        (line 1 is ``BEGIN TRANSACTION;``, as ``BufferedTransaction._locate()`` counts)."""
        first = 2
        for index, statement in enumerate(self.statements):
            last = first + statement.count("\n")
            if first <= line <= last:
                return index, line - first + 1
            first = last + 1
        return None


@dataclass(frozen=True)
class _Failure:
    """What a refused batch's answer says: the text that failed (``None`` when the answer names none), its operation
    (``None`` for the tracking statement, or no text), the server's reason without its excerpt of the statement, and
    whether the refusal is a retryable conflict."""

    statement: int | None
    owner: int | None
    reason: str
    conflict: bool


def _entry_text(entry: Any) -> str:
    return str(entry.get("result") or "") if isinstance(entry, dict) else ""


def _refused_entry(entry: Any) -> bool:
    return isinstance(entry, dict) and entry.get("status") == "ERR"


def _filler(text: str) -> bool:
    """Whether a status text only says that the statement was not executed because another one failed (2.x and 3.x
    before the failing one, 3.x's cancelled text after it, M2)."""
    return _is_pure_filler(text) or text.strip().rstrip(".") == _CANCELLED_FILLER


def _not_executed(entry: Any) -> bool:
    details = entry.get("details") if isinstance(entry, dict) else None
    return isinstance(details, dict) and details.get("kind") == "NotExecuted"


def _reason(text: str) -> str:
    """The server's reason in a status text, without any excerpt of the statement (``transaction._server_reason()``)."""
    return _server_reason(text)[0] or "the transaction failed"


def _conflicting(reason: str) -> bool:
    return is_conflict_error(SurrealDbError(reason))


def _failure(answer: Any, batch: _Batch, line: int) -> _Failure | None:
    """What *answer* — ``query_raw()``'s envelope for *batch* on the SurrealDB major *line* — says went wrong, or
    ``None`` when the batch committed (spec §5.6.1).

    A result list holds ``BEGIN``, one entry per statement and ``COMMIT`` on line 3, one entry per statement below
    (M2); a list of another length names no statement. The failing statement is the first statement entry whose
    ``details.kind`` is not ``NotExecuted`` and whose text is not filler, else the first whose text is not filler
    (``transaction._raise_for_status()``'s rule, on the statement entries). A retryable conflict in any entry — the
    3.x ``COMMIT`` entry's included — makes the failure a conflict. An ``error`` envelope (a parse error refuses the
    whole batch, M3) names the statement its last position in a statement falls in (:func:`_refused`).
    """
    if not (isinstance(answer, dict) and isinstance(answer.get("result"), list)):
        envelope = answer.get("error") if isinstance(answer, dict) else None
        message = envelope.get("message") if isinstance(envelope, dict) else None
        if isinstance(message, str) and message:
            return _refused(message, batch)
        return _Failure(None, None, f"unrecognized query_raw() response shape ({type(answer).__name__})", False)
    entries = answer["result"]
    errors = [entry for entry in entries if _refused_entry(entry)]
    if not errors:
        return None
    conflict = any(_conflicting(_reason(_entry_text(entry))) for entry in errors)
    root = next((entry for entry in errors if not _filler(_entry_text(entry))), errors[0])
    if len(entries) != batch.size + (2 if line >= 3 else 0):
        return _Failure(None, None, _reason(_entry_text(root)), conflict)
    statements = entries[1:-1] if line >= 3 else entries
    failed = [(position, entry) for position, entry in enumerate(statements) if _refused_entry(entry)]
    chosen = next((pair for pair in failed if not _not_executed(pair[1]) and not _filler(_entry_text(pair[1]))), None) or next(
        (pair for pair in failed if not _filler(_entry_text(pair[1]))), None
    )
    if chosen is None:
        return _Failure(None, None, _reason(_entry_text(root)), conflict)
    index = batch.statement_of(chosen[0])
    return _Failure(index, batch.owners[index], _reason(_entry_text(chosen[1])), conflict)


def _refused(message: str, batch: _Batch) -> _Failure:
    """The failure of a batch refused as a whole with *message* (an ``error`` envelope, or the embedded engine's
    ``RuntimeError``, M3): the reason without the server's excerpt; the text named is the one holding the last
    position falling in a text, and the position kept — the first the server gives in that text — is relative to it
    (an unclosed delimiter gives where the parser stopped, then where it opens; ``transaction._refusal()``'s rule)."""
    reason, positions = _server_reason(message)
    conflict = _conflicting(reason)
    located = [(column, place) for line, column in positions if (place := batch.locate(line)) is not None]
    if not located:
        return _Failure(None, None, reason, conflict)
    statement = located[-1][1][0]
    column, (_, own_line) = next(pair for pair in located if pair[1][0] == statement)
    at = f"(at line {own_line}, column {column} of the statement)"
    return _Failure(statement, batch.owners[statement], f"{reason} {at}", conflict)


async def _send(client: Any, batch: _Batch, line: int, name: str | None) -> _Failure | None:
    """Send *batch* with ``query_raw()`` — the tracking statement's ``$surreal_orm_migration`` bound to *name* — and
    read its answer (:func:`_failure`); ``None`` when it committed. Every cached read of the ORM is dropped around it
    (``cache.writing(None)``: a migration may change any table).

    The embedded engine's ``RuntimeError`` is its refusal of the whole batch (``transaction.py``'s rule); any other
    error — a server client's ``RuntimeError`` included — is a transport error and propagates.
    """
    variables = _encoding.encode({} if name is None else {"surreal_orm_migration": name})
    async with _cache.writing(None):
        try:
            answer = await client.query_raw(batch.text(), variables)
        except RuntimeError as error:
            embedded = _sdk.SdkAsyncEmbeddedConnection
            if embedded is None or not isinstance(client, embedded):
                raise
            return _refused(str(error), batch)
    return _failure(answer, batch, line)


async def _line(client: Any) -> Literal[2, 3]:
    """The renderers' line for *client*: 3 from SurrealDB 3 on, else 2 (``cached_major()`` answers any major)."""
    return 3 if await cached_major(client) >= 3 else 2


@dataclass(frozen=True)
class _Way:
    """What differs between applying a migration and rolling it back (spec §5.6, §7): the logs, the error texts, and
    which answer of the tracking table, read again after a failed batch, says that the work was done meanwhile."""

    #: ``True`` when a migration recorded as applied is done (``migrate()``), ``False`` when one no longer recorded is
    #: (``rollback()``).
    done_when_recorded: bool
    #: The log line before a migration's batches.
    doing: str
    #: The warning when the tracking table says the work was done meanwhile: the name, then the cause.
    skipped: str
    #: E-X1/E-X3's beginning, before ``at operation``.
    failed: str
    #: E-X2/E-X3's ``could not be recorded as …``.
    recorded_as: str
    #: E-C1's ``was not …``.
    not_done: str
    #: The method a retry runs again (E-C1's sentence).
    retry: str
    #: Who keeps the analyzer definitions phase A committed, when they could not be restored.
    keep: str


_APPLYING = _Way(
    done_when_recorded=True,
    doing="Applying migration: %s",
    skipped=(
        "Migration %s is recorded as applied although this run's batch failed (%s): a concurrent run applied it, or the "
        "answer was lost; it is not applied again, and its data operations were not run here — run upgrade() if they "
        "must."
    ),
    failed="Migration '{name}' failed",
    recorded_as="applied",
    not_done="applied",
    retry="migrate()",
    keep="this migration's definitions",
)
_ROLLING_BACK = _Way(
    done_when_recorded=False,
    doing="Rolling back: %s",
    skipped=(
        "Migration %s is no longer recorded as applied although this run's batch failed (%s): a concurrent run rolled it "
        "back, or the answer was lost; skipped."
    ),
    failed="Rollback of migration '{name}' failed",
    recorded_as="rolled back",
    not_done="rolled back",
    retry="rollback()",
    keep="the definitions this rollback restored",
)


def _statement_error(
    migration: Migration,
    failure: _Failure,
    *,
    sentence: str = "",
    restored: bool | None = None,
    way: _Way = _APPLYING,
) -> "MigrationStatementError":
    """E-X1, E-X2, E-X3 or E-X6 (spec §7) for *failure* of *migration* — applied, or rolled back with *way*
    ``_ROLLING_BACK`` —, *sentence* (the compensation's) appended."""
    name = migration.name
    tail = f" {sentence}" if sentence else ""
    if failure.statement is None:
        message = f"Migration '{name}' was refused by SurrealDB: {failure.reason}{tail}"
        return MigrationStatementError(message, migration=name, analyzers_restored=restored)
    if failure.owner is None:
        message = f"Migration '{name}' could not be recorded as {way.recorded_as}: {failure.reason}{tail}"
        return MigrationStatementError(message, migration=name, analyzers_restored=restored)
    operation = migration.operations[failure.owner]
    description = operation.describe()
    position = failure.owner + 1
    message = (
        f"{way.failed.format(name=name)} at operation {position} of {len(migration.operations)} ({description}): "
        f"{failure.reason}{tail}"
    )
    return MigrationStatementError(
        message, migration=name, operation=position, description=description, analyzers_restored=restored
    )


def _data_error(migration: Migration, failure: _Failure) -> "MigrationStatementError":
    """E-X5 (spec §7) for *failure* of *migration*'s phase C; E-X6 when the answer names no statement."""
    if failure.owner is None:
        return _statement_error(migration, _Failure(None, None, failure.reason, failure.conflict))
    operation = migration.operations[failure.owner]
    description = operation.describe()
    position = failure.owner + 1
    message = (
        f"Data migration of '{migration.name}' failed at operation {position} of {len(migration.operations)} "
        f"({description}): {failure.reason}"
    )
    return MigrationStatementError(message, migration=migration.name, operation=position, description=description)


@dataclass(frozen=True)
class _Compensated:
    """The outcome of phase A's compensation: why the analyzers could not be restored (``None``: they were), and why
    the indexes using them could not be rebuilt afterwards (``None``: they were, or there were none)."""

    analyzers: str | None
    rebuilds: str | None = None

    @property
    def restored(self) -> bool:
        return self.analyzers is None


def _compensation_sentence(outcome: _Compensated, names: Sequence[str], way: _Way = _APPLYING) -> str:
    """The sentence phase A's compensation adds (spec §7): restored, or not with its reason."""
    if outcome.analyzers is not None:
        shown = ", ".join(f"'{name}'" for name in names)
        return (
            f"The analyzer definitions it committed first could not be restored ({outcome.analyzers}): {shown} keep "
            f"{way.keep}; run schema_diff() to see them."
        )
    if outcome.rebuilds is not None:
        return (
            "The analyzer definitions it committed first were restored, but the indexes that use them could not be "
            f"rebuilt ({outcome.rebuilds}): rebuild them (REBUILD INDEX) before searching them."
        )
    return _RESTORED


# --- the pre-pass: every batch rendered before the first is sent (spec §5.6 step 3, rollback()) -----------------------


@dataclass(frozen=True)
class _Prepared:
    """One planned migration, rendered: its phase-A batch (the statements that define analyzers, ``None`` without
    one), its phase-B batch (the tracking statement, then every other schema statement), and phase A's compensation —
    the statements restoring the analyzers phase A defines, their names, and the rebuilds of the indexes the database
    holds that use them."""

    migration: Migration
    analyzers: _Batch | None
    main: _Batch
    compensation: tuple[str, ...] = ()
    restored: tuple[str, ...] = ()
    rebuilds: tuple[str, ...] = ()


def _unreadable(issues: Sequence[SchemaIssue], name: str) -> bool:
    """Whether the read could not read the analyzer *name*: an ``analyzer`` issue under that name, or the
    ``analyzers`` section issue (the read then lists no analyzer, P1)."""
    return any(
        (issue.kind == "analyzer" and issue.name == name) or (issue.kind == "section" and issue.name == "analyzers")
        for issue in issues
    )


def _unrestorable(migration: Migration, position: int, operation: DefineAnalyzer | RemoveAnalyzer) -> SurrealDbError:
    """E-I2 (spec §7, D38, P1, P3)."""
    return SurrealDbError(
        f"Migration '{migration.name}', operation {position} ({operation.describe()}): the database's definition of "
        f"analyzer '{operation.name}' cannot be read or written by the ORM, so it could not be restored if the "
        "migration failed; nothing was applied. Redefine the analyzer in a form search.Analyzer accepts (by hand, or "
        "with define_indexes()), then run the call again."
    )


def _compensation(
    migration: Migration, held: SchemaState, owners: Sequence[int]
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Phase A's compensation for *migration*, rendered from *held* — the database's schema as phase A finds it (D38):
    for ``migrate()``, the read with the planned migrations before it replayed; for ``rollback()``, the read rewound to
    the migration applied. *owners* are the operations (indexes in ``migration.operations``) whose statements phase A
    sends. For each analyzer they define, in reverse order, its definition in *held*
    (``DefineAnalyzer.from_analyzer_state(…).forwards()``, ``OVERWRITE``), or ``REMOVE ANALYZER IF EXISTS`` when
    *held* has none. ``previous=`` plays no part. Also the analyzers' names, in the migration's order, and a ``REBUILD
    INDEX IF EXISTS`` of each index of *held* that uses one of them: SurrealDB 3.x and 2.x servers keep what a
    ``REBUILD INDEX`` of the failed batch built (and 2.x what a full-text ``DEFINE INDEX OVERWRITE`` built) with phase
    A's analyzers, so the index is built again with the restored ones, once they are committed.

    :raises SurrealDbError: E-I2 — the definition cannot be read (an issue of the read) or written (a form
        ``search.Analyzer`` refuses, M32).
    """
    defined: dict[str, tuple[int, DefineAnalyzer | RemoveAnalyzer]] = {}
    for index in sorted(set(owners)):
        operation = migration.operations[index]
        if isinstance(operation, DefineAnalyzer | RemoveAnalyzer) and operation.name not in defined:
            defined[operation.name] = (index + 1, operation)
    statements: list[str] = []
    for name, (position, operation) in reversed(defined.items()):
        found = held.analyzers.get(name)
        if found is not None:
            restoring = DefineAnalyzer.from_analyzer_state(found)
            if restoring is None:
                raise _unrestorable(migration, position, operation)
            statements.append(restoring.forwards())
        elif _unreadable(held.issues, name):
            raise _unrestorable(migration, position, operation)
        else:
            statements.append(render.removal("analyzer", name, if_exists=True))
    rebuilds = tuple(
        render.rebuild(table.name, index.name, if_exists=True)
        for table in held.tables.values()
        for index in table.indexes.values()
        if index.search_analyzer in defined
    )
    return tuple(statements), tuple(defined), rebuilds


def _restores_analyzer(operation: object) -> bool:
    """Whether *operation*'s backwards defines an analyzer — a ``DefineAnalyzer`` or ``RemoveAnalyzer`` with
    ``previous=`` —, which a rollback sends in phase A (a ``DefineAnalyzer`` without one is removed in phase B, after
    the indexes that use it, M6)."""
    return isinstance(operation, DefineAnalyzer | RemoveAnalyzer) and operation.previous is not None


def _prepared(
    migration: Migration, *, line: Literal[2, 3], before: SchemaState, after: SchemaState, backwards: bool = False
) -> _Prepared:
    """*migration* rendered for *line* (``migration._statements()``: an ``AlterTable`` given ``skip_fields``).

    Forwards: each operation's ``forwards()`` from *after*, the state after the migration; the ``DefineAnalyzer`` ones
    in phase A; the ``DataMigration`` ones left out (phase C); the rest in phase B after the tracking record; phase A's
    compensation from *before*. Backwards (*backwards*): each operation's ``backwards()`` in reverse order from
    *before*, the state the migration is undone to; those that define an analyzer in phase A; the rest — a
    ``DataMigration``'s ``backwards_sql`` in its position — in phase B after the record's ``DELETE``; phase A's
    compensation from *after*, the state with the migration applied.
    """
    analyzers: list[str] = []
    analyzer_owners: list[int] = []
    main = [_UNRECORD if backwards else _RECORD]
    main_owners: list[int | None] = [None]
    rendered = _statements(migration, line=line, state=before if backwards else after, backwards=backwards)
    for index, text in rendered:
        operation = migration.operations[index]
        if not backwards and isinstance(operation, DataMigration):
            continue
        if _restores_analyzer(operation) if backwards else isinstance(operation, DefineAnalyzer):
            analyzers.append(text)
            analyzer_owners.append(index)
        else:
            main.append(text)
            main_owners.append(index)
    if not analyzers:
        return _Prepared(migration, None, _Batch(main, main_owners))
    compensation, restored, rebuilds = _compensation(migration, after if backwards else before, analyzer_owners)
    phase_a = _Batch(analyzers, analyzer_owners)
    return _Prepared(migration, phase_a, _Batch(main, main_owners), compensation, restored, rebuilds)


def _plan(migrations: Sequence[Migration], applied: Sequence[str], target: str | None) -> list[Migration]:
    """The migrations ``migrate()`` applies: the pending ones in application order, up to and including *target*;
    none when *target* is applied."""
    recorded = set(applied)
    if target is not None and target in recorded:
        return []
    plan: list[Migration] = []
    for migration in migrations:
        if migration.name not in recorded:
            plan.append(migration)
        if migration.name == target:
            break
    return plan


def _undo(migrations: Sequence[Migration], applied: Sequence[str], target: str) -> list[Migration]:
    """The migrations ``rollback()`` undoes: the applied ones after *target* in application order (every applied one
    for ``""``), newest first."""
    recorded = set(applied)
    order = [migration for migration in migrations if migration.name in recorded]
    if target:
        order = order[[migration.name for migration in order].index(target) + 1 :]
    return order[::-1]


def _irreversible(migration: Migration) -> ValueError | None:
    """The refusal of a rollback of *migration* (spec §5.6): its first irreversible operation; ``None`` when every
    operation is reversible."""
    for position, operation in enumerate(migration.operations, 1):
        if not operation.reversible:
            return ValueError(
                f"Migration {migration.name} is not reversible: operation {position} ({operation.describe()}) cannot "
                "be rolled back."
            )
    return None


class MigrationExecutor:
    """Applies, undoes and inspects the migrations of a directory (the full SurrealDB-ORM's class).

    *migrations_dir*: the directory of the migration files (``<NNNN>_<name>.py``). *connection*: the name of a
    registered connection, or ``None`` for the current one — the innermost ``using()`` block's, else ``"default"`` —,
    resolved at each call that reaches the database. Building an executor reads nothing.

    The migration files are application code: loading them runs them (point an executor at the application's own
    directory only).

    Attributes:
        migrations_dir: The migrations directory.

    :raises TypeError: *connection* is a client object, or neither a ``str`` nor ``None``.
    :raises ValueError: *connection* is ``""``.
    """

    migrations_dir: Path

    def __init__(self, migrations_dir: str | os.PathLike[str], *, connection: str | None = None) -> None:
        if connection is not None and not isinstance(connection, str) and hasattr(connection, "query"):
            raise TypeError(_CLIENT_OBJECT)
        self.migrations_dir = Path(migrations_dir)
        self._connection = None if connection is None else check_name(connection)

    async def ensure_migrations_table(self) -> None:
        """Define the tracking table ``_surreal_orm_migrations`` when it is missing — the full ORM's table, fields and
        unique index (``name`` string, ``applied_at`` datetime defaulting to ``time::now()``, index ``migration_name``
        on ``name``), each ``IF NOT EXISTS``, in one batch. A retryable conflict (two first runs at once) is retried,
        three attempts in all.

        :raises SurrealDbConflictError: the third attempt conflicted too.
        :raises SurrealDbError: SurrealDB refused the batch (a session not allowed to define, for instance).
        """
        client = await self._client()
        line = await _line(client)
        batch = _Batch(_TRACKING_DEFINITIONS, (None,) * len(_TRACKING_DEFINITIONS))
        attempt = 1
        while (failure := await _send(client, batch, line, None)) is not None:
            if not failure.conflict or attempt == _ENSURE_ATTEMPTS:
                error = SurrealDbConflictError if failure.conflict else SurrealDbError
                raise error(f"The tracking table {MIGRATIONS_TABLE} could not be defined: {failure.reason}")
            await asyncio.sleep(0.05 * attempt)
            attempt += 1

    async def get_applied_migrations(self) -> list[str]:
        """The names of the applied migrations, in the order they were applied (``applied_at``, then name) — the
        tracking table defined first when it is missing (the full ORM's, D20).

        :raises SurrealDbError: the read did not answer a list of records; see :meth:`ensure_migrations_table`.
        """
        await self.ensure_migrations_table()
        client = await self._client()
        rows = await client.query(_APPLIED, {})
        if not isinstance(rows, list):
            raise SurrealDbError(f"The tracking table {MIGRATIONS_TABLE} could not be read: unexpected answer.")
        return [row["name"] for row in rows if isinstance(row, dict) and isinstance(row.get("name"), str)]

    def get_available_migrations(self) -> list[str]:
        """The names of the directory's migration files, sorted: the stems of its ``*.py`` files that do not start
        with ``_`` and that ``parse_migration_name()`` reads (the full ORM's rule); ``[]`` for a missing directory. No
        file is imported."""
        return loader.discover(self.migrations_dir)

    def get_pending_migrations(self, applied: list[str]) -> list[str]:
        """The names of the migration files not in *applied*, in application order (every file is loaded and ordered).

        :raises ValueError: a dependency names no migration file; a dependency cycle; see :meth:`load_migration`.
        """
        recorded = set(applied)
        return [migration.name for migration in self._ordered() if migration.name not in recorded]

    def load_migration(self, name: str) -> Migration:
        """The ``migration`` that ``<migrations_dir>/<name>.py`` defines. The file is imported — run — on each call,
        and not inserted in ``sys.modules``; an error it raises propagates unchanged.

        :raises TypeError: *name* is not a ``str``.
        :raises ValueError: *name* is not a migration name, such as ``"0001_initial"`` (checked before any file is looked
            for); the file defines a migration of another name.
        :raises FileNotFoundError: ``Migration file not found: <path>``.
        :raises ImportError: ``Migration file must define 'migration' variable: <name>`` — also when ``migration`` is
            not a :class:`~surreal_orm_lite.migrations.Migration`.
        """
        return loader.load(self.migrations_dir, name)

    def migration_state(self, target: str | None = None) -> SchemaState:
        """The schema state the migration files build, up to and including *target* in application order (every file
        when ``None``) — pure (lite; Django's project state): no database is read.

        Each migration's operations are replayed in order from an empty state (the **file replay**): a removal of a
        definition no file made is a no-op, since what was defined outside migrations is unknown here. The state's
        ``applied_migrations`` lists the names replayed, in order; its definitions carry no mark.

        :raises ValueError: *target* names no migration file (``Unknown migration target '<t>': no such migration file
            in <dir>.``); see :meth:`get_pending_migrations`.
        """
        migrations = self._ordered()
        if target is not None and all(migration.name != target for migration in migrations):
            raise ValueError(f"Unknown migration target {target!r}: no such migration file in {self.migrations_dir}.")
        return _state.replay(migrations, until=target, strict=False)

    async def show_sql(self, name: str, *, backwards: bool = False, line: Literal[2, 3] = 3) -> str:
        """The statements of the migration *name*, joined by blank lines: ``forwards_sql()``'s, or with
        *backwards* ``backwards_sql()``'s, for the SurrealDB major *line* (3 by default; 2 for SurrealDB 2.x and the
        embedded engine) — pure despite ``async`` (the full ORM's signature): no request.

        The operations render from the files' state before the migration (:meth:`migration_state` of the migrations
        before it), so on line 2 the ``FLEXIBLE`` choices are those of the files, where ``migrate()`` makes them from
        the database's schema.

        :raises TypeError: *line* is not an ``int``; see :meth:`load_migration`.
        :raises ValueError: *line* is neither 2 nor 3; see :meth:`get_pending_migrations`.
        :raises FileNotFoundError: no file of the directory defines *name*.
        """
        checked = checked_line(line)
        path = loader.path_of(self.migrations_dir, name)
        migrations = self._ordered()
        names = [migration.name for migration in migrations]
        if name not in names:
            raise FileNotFoundError(f"Migration file not found: {path}")
        position = names.index(name)
        migration = migrations[position]
        before = _state.replay(migrations[:position], strict=False)
        if backwards:
            return "\n\n".join(migration.backwards_sql(line=checked, state=before))
        return "\n\n".join(migration.forwards_sql(line=checked, state=before))

    async def migrate(self, target: str | None = None, fake: bool = False, schema_only: bool = True) -> list[str]:
        """Apply the pending migrations, in application order, up to and including *target* (all when ``None``), and
        return the names applied — the full ORM's method, each migration applied atomically (spec §5.6).

        1. Every file is loaded and ordered, *target* checked, the tracking table ensured and read, and the history
           checked (an applied migration depending on a pending one raises; an applied name without a file is logged
           as a warning and ignored). Nothing to apply: ``No migrations to apply.`` is logged, ``[]`` returned.
        2. With *fake*, each migration is only recorded as applied, in a batch of its own — no DDL, no schema read, no
           data operation.
        3. Otherwise the database's schema is read **once** (``DatabaseIntrospector``), before any DDL: definitions made
           by ``define_table()`` and the other appliers, or by hand, are part of it. The planned migrations are
           replayed over it strictly, and every statement is rendered from those states — on SurrealDB 2.x and the
           embedded engine the ``FLEXIBLE`` choices are the database's —, all before the first batch is sent.
        4. Each migration: its ``DefineAnalyzer`` statements in a batch committed first (an index built in the same
           transaction would not use a redefined analyzer, M4, M26), then one batch holding its tracking record and
           every other schema statement, in order — applied whole or not at all. Its ``DataMigration`` operations run
           after, with *schema_only* ``False`` only (phase C, as :meth:`upgrade` runs them).

        When a batch fails, the tracking table is read again: a migration recorded meanwhile (a concurrent run, a lost
        answer) is skipped with a warning and left out of the names returned; otherwise a retryable conflict raises
        ``SurrealDbConflictError`` (the analyzers committed first are kept: running ``migrate()`` again converges), and a
        refusal ``MigrationStatementError``, after the analyzers committed first are restored to the database's
        definitions (``analyzers_restored``). The migrations after a failed one are not attempted.

        :param target: The last migration to apply.
        :param fake: Record the migrations as applied without running them.
        :param schema_only: ``False`` also runs each applied migration's data operations, after its schema.
        :raises ValueError: *target* names no migration file; an applied migration depends on a pending one (E-H1); a
            removal of a definition neither the database nor an earlier planned operation holds (E-R1); see
            :meth:`get_pending_migrations`.
        :raises SurrealDbError: an analyzer phase A redefines whose database definition cannot be restored (E-I2).
        :raises SurrealDbConflictError: a retryable conflict (E-C1).
        :raises MigrationStatementError: SurrealDB refused a statement (E-X1), the tracking record (E-X2), the batch
            as a whole (E-X6) or a data operation's SQL (E-X5: the migration stays applied — run :meth:`upgrade` once
            the cause is fixed). An exception a data function raises propagates unchanged.
        """
        migrations = self._ordered()
        self._check_target(migrations, target)
        applied = await self.get_applied_migrations()
        self._check_history("migrate", migrations, applied)
        plan = _plan(migrations, applied, target)
        if not plan:
            logger.info("No migrations to apply.")
            return []
        line = await _line(await self._client())
        if fake:
            prepared = [_Prepared(migration, None, _Batch([_RECORD], [None])) for migration in plan]
        else:
            prepared = await self._prepare(plan, line, backwards=False)
        done: list[str] = []
        for item in prepared:
            if not await self._apply(item, line, _APPLYING):
                continue
            if not (fake or schema_only):
                await self._data(item.migration, line)
            logger.info("Applied: %s", item.migration.name)
            done.append(item.migration.name)
        return done

    async def rollback(self, target: str) -> list[str]:
        """Undo the applied migrations after *target* in application order — newest first — and return their names:
        *target* is an applied migration (kept, with every migration before it), or ``""`` to undo every applied
        migration (the full ORM's method; spec §5.6).

        Before any DDL: every file is loaded and ordered, the tracking table ensured and read, and the history checked
        (an applied migration depending on a pending one raises, an applied name without a file too — its place in the
        order is unknown); each migration to undo must be reversible. Nothing to undo: ``[]``, no schema read. Then the
        database's schema is read **once** and rewound migration by migration (each operation's
        ``state_backwards()``), and every backwards renders from the state its migration is undone to — so on SurrealDB
        2.x and the embedded engine a table made ``SCHEMAFULL`` again redefines ``FLEXIBLE`` the columns the database
        holds, whoever defined them.

        Each migration: the analyzer definitions its backwards restore (``previous=``) in a batch committed first, then
        one batch holding the deletion of its tracking record and every other backwards in reverse order — a
        ``DataMigration``'s ``backwards_sql`` in its position —, undone whole or not at all; then each
        ``DataMigration``'s ``backwards_func``, in reverse order. A failed batch is handled as :meth:`migrate`'s: a
        migration no longer recorded meanwhile is skipped with a warning, a retryable conflict raises
        ``SurrealDbConflictError``, a refusal ``MigrationStatementError`` (E-X3) after the analyzers committed first
        are restored to the definitions the database held. The migrations after a failed one are not undone.

        :param target: The last migration to keep applied, or ``""``.
        :raises TypeError: *target* is not a ``str``.
        :raises ValueError: *target* is not applied; an applied migration depends on a pending one (E-H1); a migration
            to undo holds an irreversible operation; see :meth:`get_pending_migrations`.
        :raises FileNotFoundError: an applied migration has no file.
        :raises SurrealDbError: an analyzer the backwards restore whose database definition could not be restored
            (E-I2).
        :raises SurrealDbConflictError: a retryable conflict (E-C1).
        :raises MigrationStatementError: SurrealDB refused a statement or the record's deletion (E-X3), or the batch as
            a whole (E-X6). An exception a data function raises propagates unchanged.
        """
        if not isinstance(target, str):
            raise TypeError(
                "rollback(): target must be a str — a migration name, or '' to roll back every migration —, not "
                f"{type(target).__name__}."
            )
        migrations = self._ordered()
        applied = await self.get_applied_migrations()
        self._check_history("rollback", migrations, applied)
        if target and target not in applied:
            raise ValueError(f"Cannot roll back to {target!r}: it is not applied.")
        undo = _undo(migrations, applied, target)
        for migration in undo:
            if (refusal := _irreversible(migration)) is not None:
                raise refusal
        if not undo:
            logger.info("No migrations to rollback.")
            return []
        line = await _line(await self._client())
        done: list[str] = []
        for item in await self._prepare(undo, line, backwards=True):
            if not await self._apply(item, line, _ROLLING_BACK):
                continue
            for operation in reversed(item.migration.data_operations):
                if isinstance(operation, DataMigration) and operation.backwards_func is not None:
                    await self._call(operation.backwards_func)
            logger.info("Rolled back: %s", item.migration.name)
            done.append(item.migration.name)
        return done

    async def upgrade(self, target: str | None = None) -> list[str]:
        """Run the data operations of the applied migrations, in application order up to and including *target* (all
        when ``None``), and return the names of those that hold some — the full ORM's method (spec §5.6).

        Before any data operation: every file is loaded and ordered, *target* checked, the tracking table ensured and
        read, and the history checked (an applied migration depending on a pending one raises; an applied name without
        a file raises ``FileNotFoundError``). For each migration, the SQL of its ``DataMigration`` operations runs in
        one transaction, then each ``forwards_func`` in order — on the executor's connection. Nothing is recorded:
        running ``upgrade()`` again runs them again (write idempotent data migrations); no schema is read.

        :param target: The last migration whose data operations run.
        :raises ValueError: *target* names no migration file; an applied migration depends on a pending one (E-H1); see
            :meth:`get_pending_migrations`.
        :raises FileNotFoundError: an applied migration has no file.
        :raises MigrationStatementError: SurrealDB refused a data operation's SQL (E-X5) or the batch as a whole (E-X6).
            An exception a data function raises propagates unchanged.
        """
        migrations = self._ordered()
        self._check_target(migrations, target)
        applied = await self.get_applied_migrations()
        self._check_history("upgrade", migrations, applied)
        recorded = set(applied)
        line: Literal[2, 3] | None = None
        upgraded: list[str] = []
        for migration in migrations:
            if migration.name in recorded and migration.has_data_migrations:
                line = line or await _line(await self._client())
                logger.info("Running data migrations for: %s", migration.name)
                await self._data(migration, line)
                upgraded.append(migration.name)
            if migration.name == target:
                break
        return upgraded

    async def get_migration_status(self) -> dict[str, dict[str, bool | int]]:
        """Every migration file, in application order, with ``applied``, ``reversible``, ``has_data`` (whether it holds
        a ``DataMigration``) and ``operations`` (their number) — the full ORM's method. No schema is read.

        It never raises on the history: an applied migration whose dependency is pending, and an applied name without
        a file (which is not listed), are logged as warnings.

        :raises ValueError: a dependency names no migration file; a dependency cycle; see :meth:`load_migration`.
        """
        migrations = self._ordered()
        applied = await self.get_applied_migrations()
        self._check_history("get_migration_status", migrations, applied)
        recorded = set(applied)
        return {
            migration.name: {
                "applied": migration.name in recorded,
                "reversible": migration.is_reversible,
                "has_data": migration.has_data_migrations,
                "operations": len(migration.operations),
            }
            for migration in migrations
        }

    async def _client(self) -> Any:
        """The SDK client of the executor's connection, resolved now (the current one when it has no name)."""
        return await SurrealDBConnectionManager._manager_for(self._connection).get_client()

    def _check_target(self, migrations: Sequence[Migration], target: str | None) -> None:
        """Refuse a *target* that names no migration file (``migrate()``, ``upgrade()``), before any request."""
        if target is not None and all(migration.name != target for migration in migrations):
            raise ValueError(f"Unknown migration target {target!r}: no such migration file in {self.migrations_dir}.")

    def _check_history(self, caller: str, migrations: Sequence[Migration], applied: Sequence[str]) -> None:
        """The history checks of *caller*'s row (spec §5.6, ``loader.HISTORY``), before any write."""
        mode = loader.HISTORY[caller]
        loader.check_history(migrations, applied, self.migrations_dir, dependency=mode.dependency, unknown=mode.unknown)

    async def _prepare(self, plan: Sequence[Migration], line: Literal[2, 3], *, backwards: bool) -> list[_Prepared]:
        """The pre-pass of ``migrate()`` and ``rollback()`` (spec §5.6, §6.4): the one read of the database's schema,
        then each migration of *plan* rendered, before anything is sent.

        Forwards, *plan* is replayed over the read strictly — E-R1 raised here —: each migration renders from the state
        after it, its compensation from the state before it. Backwards, *plan* (in undo order) rewinds the read: the
        state with each migration applied is the read rewound through the migrations undone before it, and its
        backwards render from that state rewound through it (``state.rewind()``), its compensation from the state with
        it applied.
        """
        current = await DatabaseIntrospector(self._connection).introspect()
        prepared: list[_Prepared] = []
        for migration in plan:
            if backwards:
                before = _state.rewind(migration, current)
                prepared.append(_prepared(migration, line=line, before=before, after=current, backwards=True))
                current = before
            else:
                after = _state.advance(migration, current, strict=True)
                prepared.append(_prepared(migration, line=line, before=current, after=after))
                current = after
        return prepared

    async def _apply(self, item: _Prepared, line: Literal[2, 3], way: _Way) -> bool:
        """Send one prepared migration's batches — phase A, then phase B (spec §5.6) — applying it or, with *way*
        ``_ROLLING_BACK``, undoing it. ``True`` when done, ``False`` when a failed batch finds the work done meanwhile
        in the tracking table (skipped, with the warning).

        :raises SurrealDbConflictError: E-C1.
        :raises MigrationStatementError: E-X1/E-X3, E-X2, E-X6 (after the compensation, when phase A was committed).
        :raises Exception: a transport error (no answer) propagates unchanged, after the compensation when the work is
            not found done.
        """
        name = item.migration.name
        logger.info(way.doing, name)
        committed = False
        for batch in (item.analyzers, item.main):
            if batch is None:
                continue
            phase_a = batch is item.analyzers
            try:
                failure = await _send(await self._client(), batch, line, None if phase_a else name)
            except Exception:
                recorded = await self._recorded(name)
                if recorded is way.done_when_recorded:
                    logger.warning(way.skipped, name, "a lost answer")
                    return False
                if recorded is not None and item.compensation and (committed or phase_a):
                    # Phase A may have committed before its answer was lost: restoring is idempotent.
                    lost = await self._compensate(item, line)
                    logger.warning("Migration %s: %s", name, _compensation_sentence(lost, item.restored, way))
                raise
            if failure is None:
                committed = committed or phase_a
                continue
            recorded = await self._recorded(name)
            if recorded is way.done_when_recorded:
                logger.warning(way.skipped, name, "a conflict" if failure.conflict else "a refusal")
                return False
            if failure.conflict:
                kept = f" Its analyzer definitions, committed first, were kept: retry {way.retry}." if committed else ""
                raise SurrealDbConflictError(
                    f"Migration '{name}' was not {way.not_done}: a concurrent transaction conflicted (retryable): "
                    f"{failure.reason}{kept}"
                )
            if not committed:
                raise _statement_error(item.migration, failure, way=way)
            if recorded is None:
                outcome = _Compensated("the tracking table could not be read again")  # never compensated unverified
            else:
                outcome = await self._compensate(item, line)
            sentence = _compensation_sentence(outcome, item.restored, way)
            raise _statement_error(item.migration, failure, sentence=sentence, restored=outcome.restored, way=way)
        return True

    async def _data(self, migration: Migration, line: Literal[2, 3]) -> None:
        """Phase C of *migration* (spec §5.6): its ``DataMigration`` operations' SQL in one batch — no tracking
        statement —, then each ``forwards_func`` in order, on the executor's connection.

        :raises MigrationStatementError: E-X5, or E-X6 for a batch refused as a whole.
        """
        texts: list[str] = []
        owners: list[int | None] = []
        functions: list[Callable[[], Awaitable[None]]] = []
        for index, operation in enumerate(migration.operations):
            if not isinstance(operation, DataMigration):
                continue
            if text := operation.forwards(line=line):
                texts.append(text)
                owners.append(index)
            if operation.forwards_func is not None:
                functions.append(operation.forwards_func)
        if texts:
            failure = await _send(await self._client(), _Batch(texts, owners), line, None)
            if failure is not None:
                raise _data_error(migration, failure)
        for function in functions:
            await self._call(function)

    async def _call(self, function: Callable[[], Awaitable[None]]) -> None:
        """Await a data migration's *function* — inside ``using(<connection>)`` when the executor names one, so that
        the ORM calls it makes reach that connection."""
        if self._connection is None:
            await function()
            return
        async with SurrealDBConnectionManager.using(self._connection):
            await function()

    async def _recorded(self, name: str) -> bool | None:
        """Whether the tracking table records *name*, read again after a failed batch; ``None`` when that read fails
        (the connection is gone)."""
        try:
            return name in await self.get_applied_migrations()
        except Exception:
            return None

    async def _compensate(self, item: _Prepared, line: Literal[2, 3]) -> _Compensated:
        """Send phase A's compensation of *item* (spec §5.6, D38): the analyzers' batch, then, once it is committed,
        the rebuild of the indexes that use them (an index is built with the committed analyzer only, M26). Each
        failure is the server's reason, or ``the connection failed``."""
        try:
            client = await self._client()
            failure = await _send(client, _Batch(item.compensation, (None,) * len(item.compensation)), line, None)
        except Exception:
            return _Compensated("the connection failed")
        if failure is not None:
            return _Compensated(failure.reason)
        if not item.rebuilds:
            return _Compensated(None)
        try:
            failure = await _send(client, _Batch(item.rebuilds, (None,) * len(item.rebuilds)), line, None)
        except Exception:
            return _Compensated(None, "the connection failed")
        return _Compensated(None, None if failure is None else failure.reason)

    def _ordered(self) -> list[Migration]:
        """Every migration file of the directory, loaded, in application order (spec §5.6 "Loading and order")."""
        return loader.load_all(self.migrations_dir)
