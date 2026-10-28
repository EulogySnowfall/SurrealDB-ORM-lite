"""Migration system (v0.38.0): ``MigrationExecutor`` — applying migrations atomically, rolling them back, upgrading.

``migrate()`` (spec §5.6) loads and orders the migration files, ensures the tracking table ``_surreal_orm_migrations``,
reads the applied names and checks the history, then reads the database's schema **once** (``DatabaseIntrospector``),
replays the planned migrations over it strictly (E-R1 before any DDL) and renders every statement from those states
before the first one is sent. Each migration is applied in one ``BEGIN … COMMIT`` batch holding its tracking record
first (phase B); its analyzer definitions are committed in a batch of their own just before (phase A) and restored to
the definitions the database held when phase B fails (D38). A failed batch is read through the line's status layout
(``TestBatchStatus``: envelopes recorded on 3.3.4, 2.7.0 and the embedded engine), and the tracking table is read again
before anything else is decided: a concurrent run may have applied the migration, or the answer may have been lost.

``rollback()`` reads the schema once too, rewinds it migration by migration (``Operation.state_backwards()``) and renders
every backwards from those states: phase A restores analyzer definitions, phase B deletes the tracking record first and
runs the other backwards in reverse order. ``upgrade()`` and ``migrate(schema_only=False)`` run the data operations
(phase C); ``get_migration_status()`` lists every file and never raises on the history.

Unit: ``TestBatchStatus``, ``TestLine``, ``TestCompensationUnit``, ``TestRollbackUnit``, ``TestDataUnit`` (a fake
client replaying envelopes, a hand-built read). E2E — the line under test over WebSocket and HTTP, and the embedded
engine, each test in a database of its own (``migration_database()``, spec §9.2): ``TestMigrateE2E`` (AC-8, AC-13),
``TestSchemaAnchorE2E`` (AC-8 bis's hand-written part, AC-29's ``migrate()`` part), ``TestAtomicityE2E`` (AC-9),
``TestAnalyzerPhaseE2E`` (AC-10), ``TestCompensationE2E`` (AC-9, AC-9 +), ``TestConcurrencyE2E`` (AC-14),
``TestCacheE2E`` (AC-21), ``TestNoStatementLeakE2E`` (AC-22), ``TestRollbackE2E`` (AC-11, AC-18); over WebSocket and
the embedded engine only: ``TestRollbackStatesE2E`` (AC-11), ``TestReversibleE2E`` (AC-12),
``TestRollbackCompensationE2E`` (AC-9's rollback part), ``TestEventsAndFunctionsE2E`` (AC-27), ``TestDataE2E`` (AC-15,
AC-18), ``TestStatusE2E`` (AC-13, AC-18). Each E2E test chooses its expectation per line from ``cached_major()``; none
skips.

The pre-pass's refusals, before any DDL (spec §5.6 step 3, §7): unit ``TestLineRefusalsUnit`` (E-D1–E-D3),
``TestGuardsUnit`` (E-G1, E-G2), ``TestReadIssuesUnit`` (E-I1); E2E over WebSocket and the embedded engine
``TestLineDispatchE2E`` (AC-16), ``TestAlterTableFlexibleE2E`` (AC-16, M27), ``TestGuardsE2E`` (AC-17, AC-17 +),
``TestReadIssuesE2E`` (AC-16 +, AC-9 +: E-I1, E-I2 on a fake read) and ``TestReferenceE2E`` (``REFERENCE``, whose
expectation comes from the capability probe ``reference_supported()``, spec §9.2); over WebSocket only
``TestUnwritableAnalyzerE2E`` (P3, M32).
"""

import asyncio
import dataclasses
import logging
import os
import sys
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from surreal_orm_lite import (
    Analyzer,
    BaseSurrealModel,
    Index,
    QueryCache,
    SchemaMode,
    SurrealConfigDict,
    SurrealDBConnectionManager,
    _sdk,
)
from surreal_orm_lite.exceptions import SurrealDbConflictError, SurrealDbError
from surreal_orm_lite.introspection import DatabaseIntrospector
from surreal_orm_lite.migrations import (
    MIGRATIONS_TABLE,
    AddField,
    AlterField,
    AlterTable,
    CreateIndex,
    CreateTable,
    DataMigration,
    DefineAccess,
    DefineAnalyzer,
    DefineEvent,
    DefineFunction,
    DropField,
    DropIndex,
    DropTable,
    Migration,
    MigrationExecutor,
    MigrationGenerator,
    MigrationStatementError,
    Operation,
    RawSQL,
    RebuildIndex,
    RemoveAccess,
    RemoveAnalyzer,
    RemoveEvent,
    RemoveFunction,
    introspect_models,
    makemigrations,
)
from surreal_orm_lite.migrations import executor as executor_module
from surreal_orm_lite.schema_state import (
    AnalyzerState,
    FieldState,
    IndexState,
    SchemaIssue,
    SchemaState,
    TableState,
)
from surreal_orm_lite.time_travel import cached_major
from tests.conftest import _migration_url as _url
from tests.conftest import migration_database
from tests.conftest import new_database_name as _database_name
from tests.test_migration_diff import _MODEL_SET, MiBare, MiCommented, MiServer
from tests.test_migration_operations import (
    _R33_LINES,
    R33,
    R33_REVERSE,
    AdRefresh,
    AdUserTable,
    MigLikes,
    MigPerson,
    MigUser,
)

_LOGGER = "surreal_orm_lite.migrations"
_TRACKING = f"CREATE `{MIGRATIONS_TABLE}` SET name = $surreal_orm_migration;"
_FILLER = "The query was not executed due to a failed transaction"

# --- recorded status entries (M2, M3, M9; measured on 3.3.4, 2.7.0 and mem:// on 2026-10-27) -------------------------

_OK = {"result": None, "status": "OK", "time": "0ns", "type": None}
_FAILED_3 = {
    "details": {"kind": "NotExecuted"},
    "kind": "Query",
    "result": _FILLER,
    "status": "ERR",
    "time": "68.983µs",
    "type": None,
}
_CANCELLED_3 = {
    "details": {"kind": "Cancelled"},
    "kind": "Query",
    "result": "The query was not executed due to a cancelled transaction",
    "status": "ERR",
    "time": "0ns",
    "type": None,
}
_ABORTED_3 = {
    "details": {"kind": "NotExecuted"},
    "kind": "Query",
    "result": "Cannot COMMIT: the transaction was aborted due to a prior error",
    "status": "ERR",
    "time": "0ns",
    "type": None,
}
_THROWN_3 = {"kind": "Thrown", "result": "An error occurred: x", "status": "ERR", "time": "35.419µs", "type": None}
_FAILED_2 = {"result": _FILLER, "status": "ERR", "time": "389.762µs"}
_CONFLICT_COMMIT_3 = {
    "details": {"kind": "NotExecuted"},
    "kind": "Query",
    "result": "Cannot COMMIT: Transaction conflict: Write conflict, retry the transaction. This transaction can be retried",
    "status": "ERR",
    "time": "0ns",
    "type": None,
}
#: A 2.x conflict: the filler with its cause appended (M2, M9) — not pure filler.
_CONFLICT_2 = {
    "result": f"{_FILLER}. Failed to commit transaction due to a read or write conflict. This transaction can be retried",
    "status": "ERR",
    "time": "0ns",
}
_PARSE_MESSAGE_3 = (
    "Parse error: Unexpected token `an identifier`, expected ON\n --> [4:17]\n  |\n4 | DEFINE FIELD ON e TYPE;\n"
    "  |                 ^\n"
)
_PARSE_MESSAGE_2 = (
    "There was a problem with the database: Parse error: Unexpected token `an identifier`, expected ON\n --> [4:17]\n"
    "  |\n4 | DEFINE FIELD ON e TYPE;\n  |                 ^ \n"
)


def _err_3(text: str) -> dict[str, Any]:
    """A 3.x statement entry refused by the server for *text*."""
    return {"details": {"kind": "Table"}, "kind": "AlreadyExists", "result": text, "status": "ERR", "time": "1µs"}


def _err_2(text: str) -> dict[str, Any]:
    """A 2.x (and embedded) statement entry refused by the server for *text*."""
    return {"result": text, "status": "ERR", "time": "1µs"}


def _batch(*statements: str, owners: Sequence[int | None]) -> "executor_module._Batch":
    return executor_module._Batch(statements, owners)


#: The tracking record, then four operations' statements, the third of them a ``THROW`` (the measured "throw3" batch).
_FIVE = _batch(
    _TRACKING,
    "DEFINE TABLE `a` SCHEMAFULL;",
    "DEFINE FIELD `x` ON `a` TYPE int;",
    "THROW 'x';",
    "DEFINE TABLE `b`;",
    owners=[None, 0, 1, 2, 3],
)


class TestBatchStatus:
    """Spec §5.6.1: a batch's answer read by the line's layout — 3.x answers ``BEGIN`` and ``COMMIT`` entries around
    the statements', 2.x and the embedded engine the statements' only; the failing statement is the first that is not
    filler, mapped to its operation through the per-statement counts; the tracking statement is owned by none (E-X2);
    a layout of another length names no statement (E-X6); a retryable conflict in any entry is a conflict (E-C1)."""

    def test_a_committed_batch_is_no_failure(self) -> None:
        assert executor_module._failure({"result": [_OK] * 7}, _FIVE, 3) is None
        assert executor_module._failure({"result": [_OK] * 5}, _FIVE, 2) is None

    def test_a_failure_in_the_middle_on_3(self) -> None:
        answer = {"result": [_OK, _FAILED_3, _FAILED_3, _FAILED_3, _THROWN_3, _CANCELLED_3, _ABORTED_3]}
        assert executor_module._failure(answer, _FIVE, 3) == executor_module._Failure(3, 2, "An error occurred: x", False)

    def test_a_failure_in_the_middle_on_2(self) -> None:
        answer = {"result": [_FAILED_2, _FAILED_2, _FAILED_2, _err_2("An error occurred: x"), _FAILED_2]}
        assert executor_module._failure(answer, _FIVE, 2) == executor_module._Failure(3, 2, "An error occurred: x", False)

    def test_the_tracking_statement_is_owned_by_none(self) -> None:
        refused = "Database index `migration_name` already contains '0001_x', with record `_surreal_orm_migrations:a1`"
        answer = {"result": [_OK, _err_3(refused), *[_CANCELLED_3] * 4, _ABORTED_3]}
        failure = executor_module._failure(answer, _FIVE, 3)
        assert failure == executor_module._Failure(0, None, refused, False)
        assert failure is not None
        error = executor_module._statement_error(_migration("0001_x", 4), failure)
        assert str(error) == f"Migration '0001_x' could not be recorded as applied: {refused}"
        assert (error.operation, error.description, error.analyzers_restored) == (None, None, None)

    @pytest.mark.parametrize("line", [2, 3])
    def test_a_layout_of_another_length_names_no_statement(self, line: int) -> None:
        answer = {"result": [_FAILED_2, _err_2("An error occurred: x"), _FAILED_2]}
        failure = executor_module._failure(answer, _FIVE, line)
        assert failure == executor_module._Failure(None, None, "An error occurred: x", False)
        assert failure is not None
        error = executor_module._statement_error(_migration("0001_x", 4), failure)
        assert str(error) == "Migration '0001_x' was refused by SurrealDB: An error occurred: x"
        assert error.operation is None

    @pytest.mark.parametrize("line", [2, 3])
    def test_an_empty_statement_has_no_entry(self, line: int) -> None:
        batch = _batch(_TRACKING, "DEFINE TABLE `c`;;", "DEFINE TABLE `c`;", "DEFINE TABLE `d`;", owners=[None, 0, 1, 2])
        refused = "The table 'c' already exists"
        if line == 3:
            answer = {"result": [_OK, _FAILED_3, _FAILED_3, _err_3(refused), _CANCELLED_3, _ABORTED_3]}
        else:
            answer = {"result": [_FAILED_2, _FAILED_2, _err_2(refused), _FAILED_2]}
        assert executor_module._failure(answer, batch, line) == executor_module._Failure(2, 1, refused, False)

    @pytest.mark.parametrize("message", [_PARSE_MESSAGE_3, _PARSE_MESSAGE_2], ids=["3.x", "2.x"])
    def test_a_parse_error_names_the_statement_of_its_position(self, message: str) -> None:
        batch = _batch(_TRACKING, "DEFINE TABLE `e`;", "DEFINE FIELD ON e TYPE;", owners=[None, 0, 1])
        failure = executor_module._failure({"error": {"code": -32000, "message": message}}, batch, 3)
        reason = message.partition("\n")[0]
        assert failure == executor_module._Failure(2, 1, f"{reason} (at line 1, column 17 of the statement)", False)

    def test_a_parse_error_outside_every_statement_names_none(self) -> None:
        batch = _batch(_TRACKING, owners=[None])
        failure = executor_module._failure({"error": {"message": _PARSE_MESSAGE_3}}, batch, 3)
        assert failure == executor_module._Failure(None, None, _PARSE_MESSAGE_3.partition("\n")[0], False)

    def test_an_unrecognised_answer_names_none(self) -> None:
        failure = executor_module._failure(["no envelope"], _FIVE, 3)
        assert failure == executor_module._Failure(None, None, "unrecognized query_raw() response shape (list)", False)

    async def test_the_embedded_engine_refuses_with_a_runtime_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class FakeEmbedded:
            async def query_raw(self, query: str, variables: Any = None) -> Any:
                raise RuntimeError(_PARSE_MESSAGE_2)

        monkeypatch.setattr(_sdk, "SdkAsyncEmbeddedConnection", FakeEmbedded)
        batch = _batch(_TRACKING, "DEFINE TABLE `e`;", "DEFINE FIELD ON e TYPE;", owners=[None, 0, 1])
        failure = await executor_module._send(FakeEmbedded(), batch, 2, "0001_x")
        reason = _PARSE_MESSAGE_2.partition("\n")[0]
        assert failure == executor_module._Failure(2, 1, f"{reason} (at line 1, column 17 of the statement)", False)

    async def test_a_server_clients_runtime_error_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        error = RuntimeError(_PARSE_MESSAGE_2)

        class FakeEmbedded:
            pass

        class FakeServer:
            async def query_raw(self, query: str, variables: Any = None) -> Any:
                raise error

        monkeypatch.setattr(_sdk, "SdkAsyncEmbeddedConnection", FakeEmbedded)
        with pytest.raises(RuntimeError) as raised:
            await executor_module._send(FakeServer(), _FIVE, 3, "0001_x")
        assert raised.value is error

    def test_a_2x_conflict_filler_is_a_conflict(self) -> None:
        answer = {"result": [_CONFLICT_2] * 5}
        failure = executor_module._failure(answer, _FIVE, 2)
        assert failure is not None and failure.conflict is True
        assert "read or write conflict" in failure.reason

    def test_a_3x_commit_conflict_is_a_conflict(self) -> None:
        answer = {"result": [_OK, *[_OK] * 5, _CONFLICT_COMMIT_3]}
        failure = executor_module._failure(answer, _FIVE, 3)
        assert failure == executor_module._Failure(None, None, str(_CONFLICT_COMMIT_3["result"]), True)

    def test_an_alter_tables_redefinitions_count_as_its_own(self) -> None:
        """R33 on line 2: the table statement and three ``FLEXIBLE`` redefinitions, one operation; the 3rd of its four
        statements refused names the ``AlterTable``, the reason without the server's excerpt."""
        batch = _batch(_TRACKING, "\n".join(_R33_LINES), "DEFINE FIELD `bag2` ON `t` TYPE any FLEXIBLE;", owners=[None, 0, 1])
        refused = "Found a problem\n --> [1:5]\n  |\n1 | DEFINE FIELD OVERWRITE `tags` ON `t`\n"
        answer = {"result": [_FAILED_2, _FAILED_2, _FAILED_2, _err_2(refused), _FAILED_2, _FAILED_2]}
        assert executor_module._failure(answer, batch, 2) == executor_module._Failure(1, 0, "Found a problem", False)
        conflicting = {"result": [_CONFLICT_2] * 6}
        failure = executor_module._failure(conflicting, batch, 2)
        assert failure is not None and failure.conflict is True

    def test_a_statement_without_its_semicolon_is_terminated(self) -> None:
        batch = _batch(_TRACKING, "THROW 'x'", owners=[None, 0])
        assert batch.text() == f"BEGIN TRANSACTION;\n{_TRACKING}\nTHROW 'x';\nCOMMIT TRANSACTION;"


class _Versioned:
    def __init__(self, version: str) -> None:
        self._version = version

    async def version(self) -> str:
        return self._version


class TestLine:
    """Spec §5.6 step 3: the line is 3 from SurrealDB 3 on, 2 below — whatever major ``client.version()`` gives."""

    @pytest.mark.parametrize(
        ("version", "line"),
        [
            ("surrealdb-1.5.0", 2),
            ("surrealdb-2.7.0", 2),
            ("surrealdb-2.0.0", 2),
            ("surrealdb-3.3.4", 3),
            ("surrealdb-4.0.0", 3),
        ],
    )
    async def test_the_line_of_a_version(self, version: str, line: int) -> None:
        assert await executor_module._line(_Versioned(version)) == line


# --- a fake client: recorded envelopes, a hand-built read ------------------------------------------------------------

_Answer = dict[str, Any] | BaseException | Callable[["_FakeClient"], Any]


class _FakeClient:
    """An SDK client replaying envelopes: the tracking table's own batch is answered OK; every other batch by the next
    of *answers* — an envelope, an exception raised, or a callable given the client that returns either; ``SELECT``
    lists *recorded* (or raises *read_error*)."""

    def __init__(self, answers: Sequence[_Answer], *, version: str = "surrealdb-3.3.4") -> None:
        self.answers = list(answers)
        self.version_text = version
        self.recorded: list[str] = []
        self.sent: list[str] = []
        self.read_error: Exception | None = None
        self.ensure_answers: list[dict[str, Any]] = []
        self.ensured = 0
        self.rows: Any = None

    async def version(self) -> str:
        return self.version_text

    async def query_raw(self, query: str, variables: Any = None) -> Any:
        if "DEFINE TABLE IF NOT EXISTS" in query:
            self.ensured += 1
            if self.ensure_answers:
                return self.ensure_answers.pop(0)
            return _ok(4, line=3 if self.version_text.startswith("surrealdb-3") else 2)
        self.sent.append(query)
        answer = self.answers.pop(0)
        if callable(answer) and not isinstance(answer, BaseException):
            answer = answer(self)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    async def query(self, query: str, variables: Any = None) -> Any:
        if self.read_error is not None:
            raise self.read_error
        if self.rows is not None:
            return self.rows
        return [{"name": name, "applied_at": "2026-10-27T00:00:00Z"} for name in self.recorded]


def _ok(statements: int, *, line: int = 3) -> dict[str, Any]:
    """The answer of a committed batch of *statements*."""
    return {"result": [_OK] * (statements + (2 if line == 3 else 0))}


def _thrown(statements: int, at: int) -> dict[str, Any]:
    """3.x's answer to a batch of *statements* whose statement *at* (0-based) throws ``x``."""
    after = statements - at - 1
    return {"result": [_OK, *[_FAILED_3] * at, _THROWN_3, *[_CANCELLED_3] * after, _ABORTED_3]}


def _conflicted(statements: int) -> dict[str, Any]:
    """3.x's answer to a batch of *statements* refused at its commit by a conflict."""
    return {"result": [_OK, *[_OK] * statements, _CONFLICT_COMMIT_3]}


def _sent(query: str) -> list[str]:
    """The statements of a batch sent, without ``BEGIN``/``COMMIT``."""
    lines = query.split("\n")
    assert (lines[0], lines[-1]) == ("BEGIN TRANSACTION;", "COMMIT TRANSACTION;")
    return lines[1:-1]


def _migration(name: str, operations: int) -> Migration:
    """A migration named *name* holding *operations* ``CreateTable``s (for the error texts)."""
    return Migration(name=name, operations=[CreateTable(f"t{index}") for index in range(operations)])


def _write(directory: Path, name: str, operations: Sequence[Operation], dependencies: Sequence[str] = ()) -> str:
    """Write the next migration file of *directory* (``MigrationGenerator``) and return the migration's name."""
    return MigrationGenerator(directory).generate(name, list(operations), list(dependencies)).stem


def _offline(monkeypatch: pytest.MonkeyPatch, client: _FakeClient, read: SchemaState) -> None:
    """Every executor of the test talks to *client*, and reads *read* as the database's schema."""

    async def fake_client(self: MigrationExecutor) -> Any:
        return client

    async def fake_read(self: DatabaseIntrospector) -> SchemaState:
        return read

    monkeypatch.setattr(MigrationExecutor, "_client", fake_client)
    monkeypatch.setattr(DatabaseIntrospector, "introspect", fake_read)


#: The database's definition of ``english`` in the unit tests' read.
_ENGLISH_READ = AnalyzerState("english", ("blank",), ("lowercase", "snowball(english)"))
_RESTORED = "The analyzer definitions it committed first were restored."
_KEPT = "Its analyzer definitions, committed first, were kept: retry migrate()."


def _recorded_warning(name: str, cause: str) -> str:
    return (
        f"Migration {name} is recorded as applied although this run's batch failed ({cause}): a concurrent run applied "
        "it, or the answer was lost; it is not applied again, and its data operations were not run here — run "
        "upgrade() if they must."
    )


def _failing_migration(directory: Path, *, previous: bool = True) -> str:
    """``0001_a``: a ``DefineAnalyzer`` of ``english`` (phase A), a table and a refused ``RawSQL`` (phase B: the
    tracking record and two statements)."""
    redefined = DefineAnalyzer("english", ["class"], ["uppercase"]) if previous else None
    return _write(
        directory,
        "a",
        [
            DefineAnalyzer("english", ["blank"], ["lowercase"], previous=redefined),
            CreateTable("doc", "SCHEMALESS"),
            RawSQL("THROW 'x'"),
        ],
    )


def _read_with_index() -> SchemaState:
    """The unit tests' read with a table ``doc`` holding two full-text indexes on ``english`` and one on another
    analyzer."""
    indexes = {
        "doc_title": IndexState("doc_title", ("title",), search_analyzer="english", bm25=True, table="doc"),
        "odd-name": IndexState("odd-name", ("body",), search_analyzer="english", bm25=True, table="doc"),
        "doc_other": IndexState("doc_other", ("note",), search_analyzer="french", bm25=True, table="doc"),
    }
    return SchemaState(
        {"doc": TableState("doc", "SCHEMALESS", "normal", indexes=indexes)},
        analyzers={"english": _ENGLISH_READ, "french": AnalyzerState("french", ("blank",))},
    )


class TestCompensationUnit:
    """Spec §5.6 (D25, D38), §7: phase A's compensation restores the definition the **database** held — read once,
    with the planned migrations before it replayed —, never ``previous=``; a refused compensation, a conflict, a
    migration a concurrent run recorded and a lost answer, each with its text."""

    async def test_the_compensation_restores_the_databases_definition(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        name = _failing_migration(tmp_path)
        client = _FakeClient([_ok(1), _thrown(3, 2), _ok(1)])
        _offline(monkeypatch, client, SchemaState(analyzers={"english": _ENGLISH_READ}))
        with pytest.raises(MigrationStatementError) as raised:
            await MigrationExecutor(tmp_path).migrate()
        restoring = DefineAnalyzer.from_analyzer_state(_ENGLISH_READ)
        assert restoring is not None
        assert _sent(client.sent[2]) == [restoring.forwards()]
        assert _sent(client.sent[0]) == [DefineAnalyzer("english", ["blank"], ["lowercase"]).forwards()]
        assert str(raised.value) == f"Migration '{name}' failed at operation 3 of 3 (Raw SQL): An error occurred: x {_RESTORED}"
        error = raised.value
        assert (error.migration, error.operation, error.description, error.analyzers_restored) == (
            name,
            3,
            "Raw SQL",
            True,
        )

    async def test_an_analyzer_the_database_did_not_hold_is_removed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _failing_migration(tmp_path, previous=False)
        client = _FakeClient([_ok(1), _thrown(3, 2), _ok(1)])
        _offline(monkeypatch, client, SchemaState())
        with pytest.raises(MigrationStatementError) as raised:
            await MigrationExecutor(tmp_path).migrate()
        assert _sent(client.sent[2]) == ["REMOVE ANALYZER IF EXISTS `english`;"]
        assert raised.value.analyzers_restored is True

    async def test_the_second_migrations_compensation_is_the_firsts_definition(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        first = _write(tmp_path, "a", [DefineAnalyzer("english", ["blank"], ["lowercase"]), CreateTable("doc", "SCHEMALESS")])
        second = _write(tmp_path, "b", [DefineAnalyzer("english", ["blank"], ["uppercase"]), RawSQL("THROW 'x'")], [first])
        client = _FakeClient([_ok(1), _ok(2), _ok(1), _thrown(2, 1), _ok(1)])
        _offline(monkeypatch, client, SchemaState(analyzers={"english": _ENGLISH_READ}))
        with pytest.raises(MigrationStatementError) as raised:
            await MigrationExecutor(tmp_path).migrate()
        assert _sent(client.sent[4]) == [DefineAnalyzer("english", ["blank"], ["lowercase"]).forwards()]
        assert (
            str(raised.value) == f"Migration '{second}' failed at operation 2 of 2 (Raw SQL): An error occurred: x {_RESTORED}"
        )

    async def test_a_refused_compensation_is_reported(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        name = _failing_migration(tmp_path)
        client = _FakeClient([_ok(1), _thrown(3, 2), {"result": [_OK, _err_3("The analyzer is locked"), _ABORTED_3]}])
        _offline(monkeypatch, client, SchemaState(analyzers={"english": _ENGLISH_READ}))
        with pytest.raises(MigrationStatementError) as raised:
            await MigrationExecutor(tmp_path).migrate()
        assert str(raised.value) == (
            f"Migration '{name}' failed at operation 3 of 3 (Raw SQL): An error occurred: x The analyzer definitions it "
            "committed first could not be restored (The analyzer is locked): 'english' keep this migration's "
            "definitions; run schema_diff() to see them."
        )
        assert raised.value.analyzers_restored is False

    async def test_a_compensation_whose_connection_fails_is_reported(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _failing_migration(tmp_path)
        client = _FakeClient([_ok(1), _thrown(3, 2), ConnectionError("gone")])
        _offline(monkeypatch, client, SchemaState(analyzers={"english": _ENGLISH_READ}))
        with pytest.raises(MigrationStatementError) as raised:
            await MigrationExecutor(tmp_path).migrate()
        assert "could not be restored (the connection failed): 'english' keep" in str(raised.value)
        assert raised.value.analyzers_restored is False

    async def test_the_indexes_using_a_restored_analyzer_are_rebuilt(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Measured on 3.3.4 and 2.7.0 (2026-10-27): a ``REBUILD INDEX`` of a cancelled batch keeps what it built — with
        phase A's analyzer —, and on 2.7.0 a full-text ``DEFINE INDEX OVERWRITE`` too; once the analyzers are restored
        (committed), the indexes the database holds that use them are built again, in a batch of their own."""
        name = _failing_migration(tmp_path)
        client = _FakeClient([_ok(1), _thrown(3, 2), _ok(1), _ok(2)])
        _offline(monkeypatch, client, _read_with_index())
        with pytest.raises(MigrationStatementError) as raised:
            await MigrationExecutor(tmp_path).migrate()
        assert _sent(client.sent[3]) == [
            "REBUILD INDEX IF EXISTS `doc_title` ON `doc`;",
            "REBUILD INDEX IF EXISTS `odd-name` ON `doc`;",
        ]
        assert str(raised.value) == f"Migration '{name}' failed at operation 3 of 3 (Raw SQL): An error occurred: x {_RESTORED}"
        assert raised.value.analyzers_restored is True

    @pytest.mark.parametrize(
        ("answer", "reason"),
        [
            ({"result": [_OK, _err_3("The index is locked"), _FAILED_3, _ABORTED_3]}, "The index is locked"),
            (ConnectionError("gone"), "the connection failed"),
        ],
        ids=["refused", "transport"],
    )
    async def test_a_failed_rebuild_is_reported(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, answer: _Answer, reason: str
    ) -> None:
        name = _failing_migration(tmp_path)
        client = _FakeClient([_ok(1), _thrown(3, 2), _ok(1), answer])
        _offline(monkeypatch, client, _read_with_index())
        with pytest.raises(MigrationStatementError) as raised:
            await MigrationExecutor(tmp_path).migrate()
        assert str(raised.value) == (
            f"Migration '{name}' failed at operation 3 of 3 (Raw SQL): An error occurred: x The analyzer definitions it "
            f"committed first were restored, but the indexes that use them could not be rebuilt ({reason}): rebuild "
            "them (REBUILD INDEX) before searching them."
        )
        assert raised.value.analyzers_restored is True

    async def test_a_conflict_is_retryable_and_never_compensated(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        name = _failing_migration(tmp_path)
        client = _FakeClient([_ok(1), _conflicted(3)])
        _offline(monkeypatch, client, SchemaState(analyzers={"english": _ENGLISH_READ}))
        with pytest.raises(SurrealDbConflictError) as raised:
            await MigrationExecutor(tmp_path).migrate()
        assert not isinstance(raised.value, MigrationStatementError)
        assert str(raised.value) == (
            f"Migration '{name}' was not applied: a concurrent transaction conflicted (retryable): "
            f"{_CONFLICT_COMMIT_3['result']} {_KEPT}"
        )
        assert len(client.sent) == 2

    async def test_a_conflict_in_phase_a_says_nothing_was_kept(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        name = _failing_migration(tmp_path)
        client = _FakeClient([_conflicted(1)])
        _offline(monkeypatch, client, SchemaState(analyzers={"english": _ENGLISH_READ}))
        with pytest.raises(SurrealDbConflictError) as raised:
            await MigrationExecutor(tmp_path).migrate()
        assert str(raised.value) == (
            f"Migration '{name}' was not applied: a concurrent transaction conflicted (retryable): "
            f"{_CONFLICT_COMMIT_3['result']}"
        )
        assert len(client.sent) == 1

    async def test_a_refused_phase_a_restores_nothing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        name = _failing_migration(tmp_path)
        client = _FakeClient([{"result": [_OK, _err_3("The analyzer is invalid"), _ABORTED_3]}])
        _offline(monkeypatch, client, SchemaState(analyzers={"english": _ENGLISH_READ}))
        with pytest.raises(MigrationStatementError) as raised:
            await MigrationExecutor(tmp_path).migrate()
        assert str(raised.value) == (
            f"Migration '{name}' failed at operation 1 of 3 (Define analyzer english): The analyzer is invalid"
        )
        assert (raised.value.operation, raised.value.analyzers_restored) == (1, None)
        assert len(client.sent) == 1

    async def test_recorded_after_the_failure_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        name = _failing_migration(tmp_path)

        def applied_meanwhile(client: _FakeClient) -> Any:
            client.recorded.append(name)
            return _conflicted(3)

        client = _FakeClient([_ok(1), applied_meanwhile])
        _offline(monkeypatch, client, SchemaState(analyzers={"english": _ENGLISH_READ}))
        caplog.set_level(logging.INFO, logger=_LOGGER)
        assert await MigrationExecutor(tmp_path).migrate() == []
        assert _recorded_warning(name, "a conflict") in caplog.messages
        assert f"Applied: {name}" not in caplog.messages
        assert len(client.sent) == 2

    async def test_recorded_after_a_refusal_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        name = _failing_migration(tmp_path)

        def applied_meanwhile(client: _FakeClient) -> Any:
            client.recorded.append(name)
            return _thrown(3, 1)

        client = _FakeClient([_ok(1), applied_meanwhile])
        _offline(monkeypatch, client, SchemaState(analyzers={"english": _ENGLISH_READ}))
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        assert await MigrationExecutor(tmp_path).migrate() == []
        assert _recorded_warning(name, "a refusal") in caplog.messages

    async def test_a_lost_answer_found_recorded_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        name = _failing_migration(tmp_path)

        def lost(client: _FakeClient) -> Any:
            client.recorded.append(name)
            return ConnectionError("the answer was lost")

        client = _FakeClient([_ok(1), lost])
        _offline(monkeypatch, client, SchemaState(analyzers={"english": _ENGLISH_READ}))
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        assert await MigrationExecutor(tmp_path).migrate() == []
        assert _recorded_warning(name, "a lost answer") in caplog.messages
        assert len(client.sent) == 2

    async def test_a_lost_answer_not_recorded_compensates_then_propagates(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        name = _failing_migration(tmp_path)
        error = ConnectionError("the answer was lost")
        client = _FakeClient([_ok(1), error, _ok(1)])
        _offline(monkeypatch, client, SchemaState(analyzers={"english": _ENGLISH_READ}))
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        with pytest.raises(ConnectionError) as raised:
            await MigrationExecutor(tmp_path).migrate()
        assert raised.value is error
        assert len(client.sent) == 3
        assert f"Migration {name}: {_RESTORED}" in caplog.messages

    async def test_a_lost_answer_whose_reread_fails_propagates_unchanged(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _failing_migration(tmp_path)
        error = ConnectionError("the answer was lost")

        def lost(client: _FakeClient) -> Any:
            client.read_error = ConnectionError("still gone")
            return error

        client = _FakeClient([_ok(1), lost])
        _offline(monkeypatch, client, SchemaState(analyzers={"english": _ENGLISH_READ}))
        with pytest.raises(ConnectionError) as raised:
            await MigrationExecutor(tmp_path).migrate()
        assert raised.value is error
        assert len(client.sent) == 2

    async def test_a_refusal_whose_reread_fails_is_not_compensated(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        name = _failing_migration(tmp_path)

        def refused(client: _FakeClient) -> Any:
            client.read_error = ConnectionError("gone")
            return _thrown(3, 2)

        client = _FakeClient([_ok(1), refused])
        _offline(monkeypatch, client, SchemaState(analyzers={"english": _ENGLISH_READ}))
        with pytest.raises(MigrationStatementError) as raised:
            await MigrationExecutor(tmp_path).migrate()
        assert str(raised.value) == (
            f"Migration '{name}' failed at operation 3 of 3 (Raw SQL): An error occurred: x The analyzer definitions it "
            "committed first could not be restored (the tracking table could not be read again): 'english' keep this "
            "migration's definitions; run schema_diff() to see them."
        )
        assert raised.value.analyzers_restored is False
        assert len(client.sent) == 2

    @pytest.mark.parametrize(
        "issue",
        [SchemaIssue("analyzer", "english", None, "unreadable"), SchemaIssue("section", "analyzers", None, "unreadable")],
        ids=["analyzer", "section"],
    )
    async def test_an_unreadable_definition_refuses_before_any_ddl(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, issue: SchemaIssue
    ) -> None:
        name = _failing_migration(tmp_path)
        client = _FakeClient([])
        _offline(monkeypatch, client, SchemaState(issues=[issue]))
        with pytest.raises(SurrealDbError) as raised:
            await MigrationExecutor(tmp_path).migrate()
        assert str(raised.value) == _e_i2(name)
        assert client.sent == []

    async def test_an_unwritable_definition_refuses_before_any_ddl(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        name = _failing_migration(tmp_path)
        client = _FakeClient([])
        odd = AnalyzerState("english", ("blank",), ("edgengram(10,2)",))
        _offline(monkeypatch, client, SchemaState(analyzers={"english": odd}))
        with pytest.raises(SurrealDbError) as raised:
            await MigrationExecutor(tmp_path).migrate()
        assert str(raised.value) == _e_i2(name)
        assert client.sent == []


def _e_i2(name: str) -> str:
    return (
        f"Migration '{name}', operation 1 (Define analyzer english): the database's definition of analyzer 'english' "
        "cannot be read or written by the ORM, so it could not be restored if the migration failed; nothing was "
        "applied. Redefine the analyzer in a form search.Analyzer accepts (by hand, or with define_indexes()), then run "
        "the call again."
    )


class TestBatchesUnit:
    """Spec §5.6 step 4, §5.6.1: phase B's batch holds the tracking record first, then each schema operation's
    statements in order — a ``DataMigration`` left out —; and every statement is rendered from the database's read
    with the plan replayed over it (D35): on line 2 an ``AlterTable`` making a table ``SCHEMAFULL`` redefines the
    ``any`` columns the **database** holds, no migration file defining them."""

    async def test_the_record_first_and_no_data_operation(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        operations: list[Operation] = [
            CreateTable("doc", "SCHEMALESS"),
            DataMigration(forwards_sql="UPDATE doc SET a = 1;"),
            AddField("doc", "a", "int"),
        ]
        name = _write(tmp_path, "a", operations)
        client = _FakeClient([_ok(3)])
        _offline(monkeypatch, client, SchemaState())
        assert await MigrationExecutor(tmp_path).migrate() == [name]
        assert _sent(client.sent[0]) == [_TRACKING, operations[0].forwards(), operations[2].forwards()]

    @pytest.mark.parametrize("version", ["surrealdb-2.7.0", "surrealdb-3.3.4"])
    async def test_the_statements_are_rendered_from_the_read(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, version: str
    ) -> None:
        read = _read_t()
        name = _write(tmp_path, "schemafull", [R33])
        line = 3 if version.startswith("surrealdb-3") else 2
        lines = _R33_LINES if line == 2 else _R33_LINES[:1]
        client = _FakeClient([_ok(1 + len(lines), line=line)], version=version)
        _offline(monkeypatch, client, read)
        assert await MigrationExecutor(tmp_path).migrate() == [name]
        assert _sent(client.sent[0]) == [_TRACKING, *lines]


def _read_t() -> SchemaState:
    """A read holding R33's table ``t`` (``SCHEMALESS``, columns ``bag any``, ``tags array``, ``tags.* any``) that no
    migration file defines."""
    fields = {
        "bag": FieldState("bag", "any", table="t"),
        "tags": FieldState("tags", "array", nullable=False, table="t"),
        "tags.*": FieldState("tags.*", "any", table="t"),
    }
    return SchemaState({"t": TableState("t", "SCHEMALESS", "normal", fields=fields)})


class TestTrackingTable:
    """Spec §5.5: ``ensure_migrations_table()`` retries a retryable conflict (two first runs at once), three attempts
    in all; ``get_applied_migrations()`` refuses an answer that is not a list of records."""

    async def test_a_conflict_is_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeClient([])
        client.ensure_answers = [_conflicted(4), _conflicted(4)]
        _offline(monkeypatch, client, SchemaState())
        await MigrationExecutor("unused").ensure_migrations_table()
        assert client.ensured == 3

    async def test_a_third_conflict_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeClient([])
        client.ensure_answers = [_conflicted(4)] * 3
        _offline(monkeypatch, client, SchemaState())
        with pytest.raises(SurrealDbConflictError) as raised:
            await MigrationExecutor("unused").ensure_migrations_table()
        assert str(raised.value) == (
            f"The tracking table {MIGRATIONS_TABLE} could not be defined: {_CONFLICT_COMMIT_3['result']}"
        )
        assert client.ensured == 3

    async def test_a_refusal_is_not_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeClient([])
        client.ensure_answers = [{"result": [_OK, _err_3("Not enough permissions"), *[_FAILED_3] * 3, _ABORTED_3]}]
        _offline(monkeypatch, client, SchemaState())
        with pytest.raises(SurrealDbError) as raised:
            await MigrationExecutor("unused").ensure_migrations_table()
        assert not isinstance(raised.value, SurrealDbConflictError)
        assert str(raised.value) == f"The tracking table {MIGRATIONS_TABLE} could not be defined: Not enough permissions"
        assert client.ensured == 1

    async def test_an_unexpected_read_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeClient([])
        client.rows = {"name": "0001_a"}
        _offline(monkeypatch, client, SchemaState())
        with pytest.raises(SurrealDbError) as raised:
            await MigrationExecutor("unused").get_applied_migrations()
        assert str(raised.value) == f"The tracking table {MIGRATIONS_TABLE} could not be read: unexpected answer."


class TestMigrateArguments:
    """Spec §5.6 step 1: an unknown target is refused before any request."""

    async def test_an_unknown_target(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write(tmp_path, "a", [CreateTable("t")])

        async def no_client(self: MigrationExecutor) -> Any:
            raise AssertionError("no request expected")

        monkeypatch.setattr(MigrationExecutor, "_client", no_client)
        with pytest.raises(ValueError) as raised:
            await MigrationExecutor(tmp_path).migrate(target="0009_x")
        assert str(raised.value) == f"Unknown migration target '0009_x': no such migration file in {tmp_path}."


# === rollback(), upgrade(), data operations, get_migration_status() ==================================================

_UNRECORD = f"DELETE `{MIGRATIONS_TABLE}` WHERE name = $surreal_orm_migration;"
_KEPT_ROLLBACK = "Its analyzer definitions, committed first, were kept: retry rollback()."
#: ``english`` as the database held it before ``_rollback_pair()``'s second migration (its ``previous=``).
_ENGLISH_BEFORE = DefineAnalyzer("english", ["class"], ["uppercase"])


def _rolled_back_warning(name: str, cause: str) -> str:
    return (
        f"Migration {name} is no longer recorded as applied although this run's batch failed ({cause}): a concurrent "
        "run rolled it back, or the answer was lost; skipped."
    )


def _rollback_pair(directory: Path) -> tuple[str, str]:
    """``0001_a`` creates ``doc``; ``0002_b`` redefines ``english`` (``previous=``), adds a field and holds a data
    migration whose ``backwards_sql`` throws — rolled back: phase A, then the ``DELETE`` and two statements."""
    first = _write(directory, "a", [CreateTable("doc", "SCHEMALESS")])
    second = _write(
        directory,
        "b",
        [
            DefineAnalyzer("english", ["blank"], ["lowercase"], previous=_ENGLISH_BEFORE),
            AddField("doc", "a", "int"),
            DataMigration(forwards_sql="UPDATE doc SET a = 1;", backwards_sql="THROW 'x';"),
        ],
        [first],
    )
    return first, second


def _read_applied() -> SchemaState:
    """The unit tests' read with ``_rollback_pair()``'s two migrations applied: ``doc`` holds ``a``; ``english`` is the
    database's definition."""
    fields = {"a": FieldState("a", "int", table="doc")}
    return SchemaState({"doc": TableState("doc", "SCHEMALESS", "normal", fields=fields)}, analyzers={"english": _ENGLISH_READ})


def _recording(answers: Sequence[_Answer], *recorded: str, version: str = "surrealdb-3.3.4") -> _FakeClient:
    """A fake client whose tracking table records *recorded*."""
    client = _FakeClient(answers, version=version)
    client.recorded = list(recorded)
    return client


def _restoring() -> str:
    restoring = DefineAnalyzer.from_analyzer_state(_ENGLISH_READ)
    assert restoring is not None
    return restoring.forwards()


class TestRollbackUnit:
    """Spec §5.6 ``rollback()``, §7 E-X3, D38: each migration undone in phase A — the analyzer definitions its
    backwards restore (``previous=``) — then phase B — the record's ``DELETE`` first, then every other backwards in
    reverse order, a ``DataMigration``'s ``backwards_sql`` in its position —, all rendered from the read rewound; a
    failure handled as ``migrate()``'s, with the rollback's texts; phase A's compensation restores the definition the
    database held with the migration applied."""

    async def test_the_batches_and_the_compensation(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        first, second = _rollback_pair(tmp_path)
        client = _recording([_ok(1), _thrown(3, 1), _ok(1)], first, second)
        _offline(monkeypatch, client, _read_applied())
        with pytest.raises(MigrationStatementError) as raised:
            await MigrationExecutor(tmp_path).rollback(first)
        assert _sent(client.sent[0]) == [_ENGLISH_BEFORE.forwards()]
        assert _sent(client.sent[1]) == [_UNRECORD, "THROW 'x';", "REMOVE FIELD IF EXISTS `a` ON `doc`;"]
        assert _sent(client.sent[2]) == [_restoring()]
        assert str(raised.value) == (
            f"Rollback of migration '{second}' failed at operation 3 of 3 (Data migration): An error occurred: x {_RESTORED}"
        )
        error = raised.value
        assert (error.migration, error.operation, error.description, error.analyzers_restored) == (
            second,
            3,
            "Data migration",
            True,
        )

    async def test_a_refused_compensation_keeps_the_restored_definitions(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        first, second = _rollback_pair(tmp_path)
        refused = {"result": [_OK, _err_3("The analyzer is locked"), _ABORTED_3]}
        client = _recording([_ok(1), _thrown(3, 1), refused], first, second)
        _offline(monkeypatch, client, _read_applied())
        with pytest.raises(MigrationStatementError) as raised:
            await MigrationExecutor(tmp_path).rollback(first)
        assert str(raised.value) == (
            f"Rollback of migration '{second}' failed at operation 3 of 3 (Data migration): An error occurred: x The "
            "analyzer definitions it committed first could not be restored (The analyzer is locked): 'english' keep the "
            "definitions this rollback restored; run schema_diff() to see them."
        )
        assert raised.value.analyzers_restored is False

    async def test_a_conflict_is_retryable_and_never_compensated(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        first, second = _rollback_pair(tmp_path)
        client = _recording([_ok(1), _conflicted(3)], first, second)
        _offline(monkeypatch, client, _read_applied())
        with pytest.raises(SurrealDbConflictError) as raised:
            await MigrationExecutor(tmp_path).rollback(first)
        assert not isinstance(raised.value, MigrationStatementError)
        assert str(raised.value) == (
            f"Migration '{second}' was not rolled back: a concurrent transaction conflicted (retryable): "
            f"{_CONFLICT_COMMIT_3['result']} {_KEPT_ROLLBACK}"
        )
        assert len(client.sent) == 2

    async def test_a_refused_delete_is_reported(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        first, second = _rollback_pair(tmp_path)
        refused = {"result": [_OK, _err_3("Not enough permissions"), _CANCELLED_3, _CANCELLED_3, _ABORTED_3]}
        client = _recording([_ok(1), refused, _ok(1)], first, second)
        _offline(monkeypatch, client, _read_applied())
        with pytest.raises(MigrationStatementError) as raised:
            await MigrationExecutor(tmp_path).rollback(first)
        assert str(raised.value) == (
            f"Migration '{second}' could not be recorded as rolled back: Not enough permissions {_RESTORED}"
        )
        assert (raised.value.operation, raised.value.analyzers_restored) == (None, True)

    async def test_a_refused_batch_without_phase_a(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        first = _write(tmp_path, "a", [CreateTable("doc", "SCHEMALESS"), CreateTable("log", "SCHEMALESS")])
        client = _recording([_thrown(3, 2)], first)
        _offline(monkeypatch, client, SchemaState())
        with pytest.raises(MigrationStatementError) as raised:
            await MigrationExecutor(tmp_path).rollback("")
        assert _sent(client.sent[0]) == [_UNRECORD, "REMOVE TABLE IF EXISTS `log`;", "REMOVE TABLE IF EXISTS `doc`;"]
        assert str(raised.value) == (
            f"Rollback of migration '{first}' failed at operation 1 of 2 (Create table doc): An error occurred: x"
        )
        assert (raised.value.operation, raised.value.analyzers_restored) == (1, None)

    @pytest.mark.parametrize("cause", ["a conflict", "a refusal"])
    async def test_no_longer_recorded_after_the_failure_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture, cause: str
    ) -> None:
        first, second = _rollback_pair(tmp_path)

        def rolled_back_meanwhile(client: _FakeClient) -> Any:
            client.recorded.remove(second)
            return _conflicted(3) if cause == "a conflict" else _thrown(3, 1)

        client = _recording([_ok(1), rolled_back_meanwhile], first, second)
        _offline(monkeypatch, client, _read_applied())
        caplog.set_level(logging.INFO, logger=_LOGGER)
        assert await MigrationExecutor(tmp_path).rollback(first) == []
        assert _rolled_back_warning(second, cause) in caplog.messages
        assert f"Rolled back: {second}" not in caplog.messages
        assert len(client.sent) == 2

    async def test_a_lost_answer_no_longer_recorded_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        first, second = _rollback_pair(tmp_path)

        def lost(client: _FakeClient) -> Any:
            client.recorded.remove(second)
            return ConnectionError("the answer was lost")

        client = _recording([_ok(1), lost], first, second)
        _offline(monkeypatch, client, _read_applied())
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        assert await MigrationExecutor(tmp_path).rollback(first) == []
        assert _rolled_back_warning(second, "a lost answer") in caplog.messages

    async def test_a_lost_answer_still_recorded_compensates_then_propagates(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        first, second = _rollback_pair(tmp_path)
        error = ConnectionError("the answer was lost")
        client = _recording([_ok(1), error, _ok(1)], first, second)
        _offline(monkeypatch, client, _read_applied())
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        with pytest.raises(ConnectionError) as raised:
            await MigrationExecutor(tmp_path).rollback(first)
        assert raised.value is error
        assert _sent(client.sent[2]) == [_restoring()]
        assert f"Migration {second}: {_RESTORED}" in caplog.messages

    @pytest.mark.parametrize(
        ("operation", "issue"),
        [
            (
                DefineAnalyzer("english", ["blank"], previous=_ENGLISH_BEFORE),
                SchemaIssue("analyzer", "english", None, "unreadable"),
            ),
            (RemoveAnalyzer("english", previous=_ENGLISH_BEFORE), SchemaIssue("section", "analyzers", None, "unreadable")),
        ],
        ids=["define", "remove"],
    )
    async def test_an_unrestorable_definition_refuses_before_any_ddl(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, operation: Operation, issue: SchemaIssue
    ) -> None:
        name = _write(tmp_path, "a", [CreateTable("doc", "SCHEMALESS"), operation])
        client = _recording([], name)
        _offline(monkeypatch, client, SchemaState(issues=[issue]))
        with pytest.raises(SurrealDbError) as raised:
            await MigrationExecutor(tmp_path).rollback("")
        assert str(raised.value) == (
            f"Migration '{name}', operation 2 ({operation.describe()}): the database's definition of analyzer "
            "'english' cannot be read or written by the ORM, so it could not be restored if the migration failed; "
            "nothing was applied. Redefine the analyzer in a form search.Analyzer accepts (by hand, or with "
            "define_indexes()), then run the call again."
        )
        assert client.sent == []

    async def test_a_removed_analyzer_is_compensated_by_its_removal(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        name = _write(tmp_path, "a", [RemoveAnalyzer("english", previous=_ENGLISH_BEFORE), RawSQL("UPDATE a;", "THROW 'x';")])
        client = _recording([_ok(1), _thrown(2, 1), _ok(1)], name)
        _offline(monkeypatch, client, SchemaState())
        with pytest.raises(MigrationStatementError) as raised:
            await MigrationExecutor(tmp_path).rollback("")
        assert _sent(client.sent[0]) == [_ENGLISH_BEFORE.forwards()]
        assert _sent(client.sent[2]) == ["REMOVE ANALYZER IF EXISTS `english`;"]
        assert raised.value.analyzers_restored is True

    @pytest.mark.parametrize("version", ["surrealdb-2.7.0", "surrealdb-3.3.4"])
    async def test_the_backwards_render_from_the_read_rewound(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, version: str
    ) -> None:
        """D35: the read holds ``t``'s columns — no migration file defines them —, so on line 2 the backwards that
        restores ``SCHEMAFULL`` redefines them ``FLEXIBLE`` (R33's lines); on line 3 the table statement comes alone."""
        name = _write(tmp_path, "schemaless", [R33_REVERSE])
        line = 3 if version.startswith("surrealdb-3") else 2
        lines = _R33_LINES if line == 2 else _R33_LINES[:1]
        client = _recording([_ok(1 + len(lines), line=line)], name, version=version)
        _offline(monkeypatch, client, _read_t())
        assert await MigrationExecutor(tmp_path).rollback("") == [name]
        assert _sent(client.sent[0]) == [_UNRECORD, *lines]

    async def test_an_irreversible_migration_refuses_before_any_request(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        first = _write(tmp_path, "a", [CreateTable("doc", "SCHEMALESS")])
        second = _write(tmp_path, "b", [CreateTable("x", "SCHEMALESS"), DropTable("x")], [first])
        client = _recording([], first, second)
        _offline(monkeypatch, client, SchemaState())
        with pytest.raises(ValueError) as raised:
            await MigrationExecutor(tmp_path).rollback("")
        assert str(raised.value) == f"Migration {second} is not reversible: operation 2 (Drop table x) cannot be rolled back."
        assert client.sent == []

    async def test_a_target_that_is_not_a_str(self, tmp_path: Path) -> None:
        with pytest.raises(TypeError) as raised:
            await MigrationExecutor(tmp_path).rollback(None)  # type: ignore[arg-type]
        assert str(raised.value) == (
            "rollback(): target must be a str — a migration name, or '' to roll back every migration —, not NoneType."
        )

    async def test_a_data_migration_without_a_backwards_function(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        name = _write(
            tmp_path,
            "a",
            [
                CreateTable("doc", "SCHEMALESS"),
                DataMigration(forwards_sql="UPDATE doc SET a = 1;", backwards_sql="UPDATE doc SET a = 0;"),
            ],
        )
        client = _recording([_ok(3)], name)
        _offline(monkeypatch, client, SchemaState())
        caplog.set_level(logging.INFO, logger=_LOGGER)
        assert await MigrationExecutor(tmp_path).rollback("") == [name]
        assert _sent(client.sent[0]) == [_UNRECORD, "UPDATE doc SET a = 0;", "REMOVE TABLE IF EXISTS `doc`;"]
        assert caplog.messages == [f"Rolling back: {name}", f"Rolled back: {name}"]

    async def test_one_compensation_per_analyzer_name(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Two operations of one migration on the same analyzer: one restoring statement, named after the first."""
        name = _write(
            tmp_path,
            "a",
            [
                DefineAnalyzer("english", ["blank"], ["lowercase"], previous=_ENGLISH_BEFORE),
                DefineAnalyzer("english", ["blank"], ["ascii"], previous=DefineAnalyzer("english", ["blank"], ["lowercase"])),
                RawSQL("UPDATE a;", "THROW 'x';"),
            ],
        )
        client = _recording([_ok(2), _thrown(2, 1), _ok(1)], name)
        _offline(monkeypatch, client, SchemaState(analyzers={"english": _ENGLISH_READ}))
        with pytest.raises(MigrationStatementError) as raised:
            await MigrationExecutor(tmp_path).rollback("")
        assert _sent(client.sent[2]) == [_restoring()]
        assert raised.value.analyzers_restored is True


class TestDataUnit:
    """Spec §5.6 phase C, §7 E-X5: the data operations' SQL in one batch, then their functions; a refusal names the
    operation; a batch refused as a whole names none."""

    async def test_a_refused_statement_names_its_operation(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        name = _write(
            tmp_path,
            "a",
            [
                CreateTable("doc", "SCHEMALESS"),
                DataMigration(forwards_sql="UPDATE doc SET a = 1;", description="Set a"),
                DataMigration(forwards_sql="THROW 'x';", description="Throw"),
            ],
        )
        client = _recording([_thrown(2, 1)], name)
        _offline(monkeypatch, client, SchemaState())
        with pytest.raises(MigrationStatementError) as raised:
            await MigrationExecutor(tmp_path).upgrade()
        assert _sent(client.sent[0]) == ["UPDATE doc SET a = 1;", "THROW 'x';"]
        assert str(raised.value) == f"Data migration of '{name}' failed at operation 3 of 3 (Throw): An error occurred: x"
        assert (raised.value.migration, raised.value.operation, raised.value.description) == (name, 3, "Throw")

    async def test_a_batch_refused_as_a_whole(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        name = _write(tmp_path, "a", [DataMigration(forwards_sql="UPDATE doc SET a = 1;")])
        client = _recording([{"result": [_err_3("x")]}], name)
        _offline(monkeypatch, client, SchemaState())
        with pytest.raises(MigrationStatementError) as raised:
            await MigrationExecutor(tmp_path).upgrade()
        assert str(raised.value) == f"Migration '{name}' was refused by SurrealDB: x"
        assert raised.value.operation is None

    async def test_upgrade_with_an_unknown_target(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _write(tmp_path, "a", [CreateTable("t")])

        async def no_client(self: MigrationExecutor) -> Any:
            raise AssertionError("no request expected")

        monkeypatch.setattr(MigrationExecutor, "_client", no_client)
        with pytest.raises(ValueError) as raised:
            await MigrationExecutor(tmp_path).upgrade(target="0009_x")
        assert str(raised.value) == f"Unknown migration target '0009_x': no such migration file in {tmp_path}."

    async def test_a_function_alone_sends_no_batch(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """A ``DataMigration`` holding only a function sends no SQL; the function runs inside ``using()`` of the
        executor's connection (it records the active connection name next to its file)."""
        name = "0001_data"
        (tmp_path / f"{name}.py").write_text(_FUNCTION_FILE.format(name=name), encoding="utf-8")
        SurrealDBConnectionManager.add_connection(
            "du_other", url="ws://localhost:1/rpc", user="root", password="root", namespace="ns", database="db"
        )
        client = _recording([], name)
        _offline(monkeypatch, client, SchemaState())
        assert await MigrationExecutor(tmp_path, connection="du_other").upgrade() == [name]
        assert client.sent == []
        assert (tmp_path / f"{name}.ran").read_text(encoding="utf-8") == "du_other"


#: A hand-written migration whose only operation is a data function: it writes the active connection's name to
#: ``<file>.ran``.
_FUNCTION_FILE = """from pathlib import Path

from surreal_orm_lite import SurrealDBConnectionManager
from surreal_orm_lite.migrations import Migration
from surreal_orm_lite.migrations.operations import DataMigration


async def forwards() -> None:
    Path(__file__).with_suffix(".ran").write_text(str(SurrealDBConnectionManager.get_active_connection_name()), encoding="utf-8")


migration = Migration(name={name!r}, operations=[DataMigration(forwards_func=forwards)])
"""


# === E2E: the line under test over WebSocket and HTTP, and the embedded engine ========================================

#: The engines every E2E class runs on.
_KINDS = ["ws", "http", "mem"]


async def _line(client: Any) -> int:
    return 3 if await cached_major(client) >= 3 else 2


async def _run(client: Any, statements: str) -> None:
    """Run *statements* by hand, every status checked."""
    answer = await client.query_raw(statements, {})
    assert "error" not in answer, answer["error"]
    assert all(entry["status"] == "OK" for entry in answer["result"]), answer["result"]


async def _info_db(client: Any) -> dict[str, Any]:
    info = await client.query("INFO FOR DB;", {})
    assert isinstance(info, dict)
    return info


async def _tables(client: Any) -> set[str]:
    return set((await _info_db(client))["tables"])


async def _fields(client: Any, table: str) -> dict[str, str]:
    info = await client.query(f"INFO FOR TABLE `{table}`;", {})
    assert isinstance(info, dict)
    return dict(info["fields"])


async def _record(client: Any, name: str) -> None:
    """A tracking record written by hand (a bound ``CREATE``)."""
    answer = await client.query_raw(f"CREATE `{MIGRATIONS_TABLE}` SET name = $name;", {"name": name})
    assert [entry["status"] for entry in answer["result"]] == ["OK"], answer


def _read_spy(monkeypatch: pytest.MonkeyPatch) -> list[None]:
    """One item per ``DatabaseIntrospector.introspect()`` call; the real read is made."""
    calls: list[None] = []
    real = DatabaseIntrospector.introspect

    async def spy(self: DatabaseIntrospector) -> SchemaState:
        calls.append(None)
        return await real(self)

    monkeypatch.setattr(DatabaseIntrospector, "introspect", spy)
    return calls


class TestMigrateE2E:
    """AC-8, AC-13 (``migrate()``'s part): the initial migration of every model kind applied, the database then equal
    to the models' state; ``target=``, ``fake=True``, a run with nothing to apply; the history checks of ``migrate()``'s
    row (spec §5.6); the schema read once per run that plans something, never otherwise."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_initial_migration_of_every_model_kind(
        self, kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        async with migration_database(kind) as client:
            line = await _line(client)
            # Refresh tokens (WITH REFRESH) need SurrealDB 3.x: below, the model set leaves that user model out.
            models = [model for model in _MODEL_SET if line >= 3 or model is not AdRefresh]
            path = await makemigrations("initial", migrations_dir=tmp_path, models=models, from_db=False)
            assert path is not None and path.stem == "0001_initial"
            reads = _read_spy(monkeypatch)
            caplog.set_level(logging.INFO, logger=_LOGGER)
            executor = MigrationExecutor(tmp_path)
            assert await executor.migrate() == ["0001_initial"]
            assert len(reads) == 1
            assert caplog.messages == ["Applying migration: 0001_initial", "Applied: 0001_initial"]
            assert await executor.get_applied_migrations() == ["0001_initial"]
            read = await DatabaseIntrospector().introspect()
            assert list(read.issues) == []
            assert read.diff(introspect_models(models)) == []
            ann = MigPerson(name="Ann", email="ann@x.io", emb=[0.1, 0.2, 0.3])
            bob = MigPerson(name="Bob", email="bob@x.io", emb=[0.3, 0.2, 0.1])
            await ann.save()
            await bob.save()
            await ann.relate(MigLikes, bob)
            saved: list[BaseSurrealModel] = [
                MiBare(label="b"),
                MiCommented(data={"a": 1}),
                MiServer(note="n"),
                MigUser(email="u@x.io", password="p"),  # its record id is its primary key
                AdUserTable(email="t@x.io", password="p"),
            ]
            for instance in saved:
                await instance.save()
            assert await client.query("SELECT n FROM mig_stats;", {}) == [{"n": 2}]
            assert len(await client.query("SELECT * FROM mig_likes;", {})) == 1
            # A SIGNUP through the accesses migrate() defined — the primary-key form (#338) included. On the embedded
            # engine the exchange runs on the shared session (bind=True), so last. (AdPlain, MiSchemaless and AdRefresh
            # declare no id: no record of theirs can be read into an instance; the diff above checks their tables and
            # accesses.)
            for model in (AdUserTable, MigUser):
                email = f"{model.__name__.lower()}@x.io"
                result = await model.signup(email=email, password="p", bind=kind == "mem")
                assert result.user.email == email

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_target_then_the_rest_then_nothing(
        self, kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        async with migration_database(kind) as client:
            first = _write(tmp_path, "a", [CreateTable("mt_a", "SCHEMALESS")])
            second = _write(tmp_path, "b", [AddField("mt_a", "n", "int")], [first])
            third = _write(tmp_path, "c", [CreateTable("mt_c", "SCHEMALESS")], [second])
            executor = MigrationExecutor(tmp_path)
            reads = _read_spy(monkeypatch)
            assert await executor.migrate(target=second) == [first, second]
            assert len(reads) == 1
            assert await executor.get_applied_migrations() == [first, second]
            assert "n" in await _fields(client, "mt_a")
            assert "mt_c" not in await _tables(client)
            assert await executor.migrate(target=first) == []
            assert len(reads) == 1
            assert await executor.migrate() == [third]
            assert len(reads) == 2
            assert await executor.get_applied_migrations() == [first, second, third]
            caplog.set_level(logging.INFO, logger=_LOGGER)
            caplog.clear()
            assert await executor.migrate() == []
            assert len(reads) == 2
            assert caplog.messages == ["No migrations to apply."]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_fake_records_without_ddl(self, kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        async with migration_database(kind) as client:
            first = _write(tmp_path, "a", [CreateTable("mf_a", "SCHEMALESS")])
            second = _write(tmp_path, "b", [AddField("mf_a", "n", "int"), RawSQL("THROW 'never sent'")], [first])
            reads = _read_spy(monkeypatch)
            executor = MigrationExecutor(tmp_path)
            assert await executor.migrate(fake=True) == [first, second]
            assert reads == []
            assert await executor.get_applied_migrations() == [first, second]
            assert "mf_a" not in await _tables(client)
            assert await executor.migrate() == []

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_recorded_name_without_a_file_is_ignored(
        self, kind: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        async with migration_database(kind) as client:
            first = _write(tmp_path, "a", [CreateTable("mg_a", "SCHEMALESS")])
            executor = MigrationExecutor(tmp_path)
            await executor.ensure_migrations_table()
            await _record(client, "0099_gone")
            caplog.set_level(logging.WARNING, logger=_LOGGER)
            assert await executor.migrate() == [first]
            warning = f"Migration 0099_gone is recorded as applied but has no file in {tmp_path}; it is ignored."
            assert caplog.messages.count(warning) == 1
            assert "mg_a" in await _tables(client)
            assert await executor.get_applied_migrations() == ["0099_gone", first]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_an_applied_migration_whose_dependency_is_pending(
        self, kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async with migration_database(kind) as client:
            first = _write(tmp_path, "a", [CreateTable("mh_a", "SCHEMALESS")])
            second = _write(tmp_path, "b", [CreateTable("mh_b", "SCHEMALESS")], [first])
            executor = MigrationExecutor(tmp_path)
            await executor.ensure_migrations_table()
            await _record(client, second)
            before = await _info_db(client)
            reads = _read_spy(monkeypatch)
            with pytest.raises(ValueError) as raised:
                await executor.migrate()
            assert str(raised.value) == (
                f"Migration '{second}' is applied but its dependency '{first}' is not: the database's history does not "
                f"match {tmp_path}."
            )
            assert await _info_db(client) == before
            assert reads == []

    async def test_get_applied_migrations_creates_the_tracking_table(self) -> None:
        async with migration_database("ws") as client:
            assert MIGRATIONS_TABLE not in await _tables(client)
            executor = MigrationExecutor("unused")
            assert await executor.get_applied_migrations() == []
            fields = await _fields(client, MIGRATIONS_TABLE)
            assert set(fields) == {"name", "applied_at"}
            await executor.ensure_migrations_table()
            assert await executor.get_applied_migrations() == []


class AnTable(BaseSurrealModel):
    """A SCHEMAFULL table made by ``define_table()``, no migration file creating it."""

    model_config = SurrealConfigDict(table_name="an_t", schema_mode=SchemaMode.SCHEMAFULL)

    id: str | None = None
    name: str = ""
    age: int = 0


class AnLoose(BaseSurrealModel):
    """A SCHEMALESS table made by ``define_table()`` with an ``Any`` column."""

    model_config = SurrealConfigDict(table_name="an_s")

    id: str | None = None
    bag: Any = None


class TestSchemaAnchorE2E:
    """C8, D35 (AC-8 bis's hand-written part, AC-29's ``migrate()`` part): the plan is replayed over the database's
    schema, so a migration file can alter a table ``define_table()`` made; a removal of a definition neither the
    database nor an earlier migration holds is refused before any DDL (E-R1); ``show_sql()`` stays pure."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_field_added_to_a_table_define_table_made(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind) as client:
            await AnTable.define_table()
            name = _write(tmp_path, "add_email", [AddField("an_t", "email", "string"), AddField("an_t", "bag", "any")])
            assert await MigrationExecutor(tmp_path).migrate() == [name]
            fields = await _fields(client, "an_t")
            assert {"name", "age", "email", "bag"} <= set(fields)
            # The table's schema mode is read from the database: below 3 an ``any`` column of a SCHEMAFULL table is
            # FLEXIBLE (v0.34.0), never on 3.x.
            assert ("FLEXIBLE" in fields["bag"]) is (await _line(client) < 3)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_column_define_table_made_altered(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind) as client:
            await AnTable.define_table()
            name = _write(tmp_path, "age_text", [AlterField("an_t", "age", "string", previous_type="int")])
            assert await MigrationExecutor(tmp_path).migrate() == [name]
            assert "TYPE string" in (await _fields(client, "an_t"))["age"]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_tables_with_non_identifier_names_dropped(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind) as client:
            await _run(
                client,
                "DEFINE TABLE `user-events` SCHEMAFULL; DEFINE FIELD `first-name` ON `user-events` TYPE string; "
                "DEFINE INDEX `idx-x` ON `user-events` FIELDS `first-name`; DEFINE TABLE `2024_sales`;",
            )
            operations = [DropTable("user-events"), DropTable("2024_sales")]
            assert operations[0].describe() == "Drop table 'user-events'"
            name = _write(tmp_path, "drop", operations)
            assert await MigrationExecutor(tmp_path).migrate() == [name]
            assert not {"user-events", "2024_sales"} & await _tables(client)

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_removal_of_nothing_is_refused_before_any_ddl(
        self, kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async with migration_database(kind) as client:
            await AnTable.define_table()
            name = _write(tmp_path, "drop_nope", [CreateTable("an_new"), DropField("an_t", "nope")])
            executor = MigrationExecutor(tmp_path)
            await executor.ensure_migrations_table()
            before = await _info_db(client)
            reads = _read_spy(monkeypatch)
            with pytest.raises(ValueError) as raised:
                await executor.migrate()
            assert str(raised.value) == (
                f"Migration '{name}', operation 2 (Drop field nope from an_t): field 'nope' of table 'an_t' is neither "
                "in the database nor defined by an earlier migration, so SurrealDB would refuse the removal. A RawSQL "
                "that defines it declares that in state_operations=."
            )
            assert len(reads) == 1
            assert await _info_db(client) == before
            assert await executor.get_applied_migrations() == []

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_show_sql_sends_no_request(self, kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        async with migration_database(kind) as client:
            await AnLoose.define_table()
            first = _write(tmp_path, "add_email", [AddField("an_s", "email", "string")])
            schemafull = AlterTable("an_s", "SCHEMAFULL", "normal", previous_schema_mode="SCHEMALESS")
            second = _write(tmp_path, "schemafull", [schemafull], [first])
            requests: list[str] = []

            async def spy(*args: Any, **kwargs: Any) -> Any:
                requests.append(str(args[:1]))
                raise AssertionError("no request expected")

            for method in ("query", "query_raw", "version"):
                monkeypatch.setattr(client, method, spy)
            reads = _read_spy(monkeypatch)
            executor = MigrationExecutor(tmp_path)
            assert await executor.show_sql(first) == AddField("an_s", "email", "string").forwards()
            # The files hold no column of an_s: on line 2 the table statement comes alone.
            shown = await executor.show_sql(second, line=2)
            assert shown == schemafull.forwards(line=2)
            assert "DEFINE FIELD" not in shown
            assert requests == []
            assert reads == []
            monkeypatch.undo()  # the database is removed with the real client


class TestAtomicityE2E:
    """AC-9 (first part): a migration is applied whole or not at all, its tracking record with it; fixed, it is applied
    from the start; the tracking record is written first, so a ``LET`` in a later ``RawSQL`` cannot change it (M25)."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_thrown_third_operation_leaves_nothing(
        self, kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "dont_write_bytecode", True)  # the file is written again below
        async with migration_database(kind) as client:
            operations: list[Operation] = [
                CreateTable("at_t", "SCHEMAFULL"),
                AddField("at_t", "a", "int"),
                RawSQL("THROW 'boom'"),
                AddField("at_t", "b", "string"),
                CreateIndex("at_t", "at_a", ["a"]),
            ]
            name = _write(tmp_path, "atomic", operations)
            executor = MigrationExecutor(tmp_path)
            with pytest.raises(MigrationStatementError) as raised:
                await executor.migrate()
            assert str(raised.value) == f"Migration '{name}' failed at operation 3 of 5 (Raw SQL): An error occurred: boom"
            assert (raised.value.migration, raised.value.operation, raised.value.description) == (name, 3, "Raw SQL")
            assert raised.value.analyzers_restored is None
            assert "at_t" not in await _tables(client)
            assert await executor.get_applied_migrations() == []
            (tmp_path / f"{name}.py").unlink()
            assert (
                _write(tmp_path, "atomic", [operation for operation in operations if not isinstance(operation, RawSQL)]) == name
            )
            assert await executor.migrate() == [name]
            assert {"a", "b"} <= set(await _fields(client, "at_t"))
            assert await executor.get_applied_migrations() == [name]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_field_defined_by_hand_refuses_the_migration(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind) as client:
            await _run(client, "DEFINE TABLE at_h SCHEMALESS; DEFINE FIELD c ON at_h TYPE int;")
            name = _write(
                tmp_path,
                "atomic",
                [
                    CreateTable("at_new", "SCHEMAFULL"),
                    AddField("at_new", "a", "int"),
                    AddField("at_h", "c", "int"),
                    AddField("at_new", "b", "string"),
                    CreateIndex("at_new", "at_new_a", ["a"]),
                ],
            )
            executor = MigrationExecutor(tmp_path)
            with pytest.raises(MigrationStatementError) as raised:
                await executor.migrate()
            assert str(raised.value) == (
                f"Migration '{name}' failed at operation 3 of 5 (Add field c to at_h): The field 'c' already exists"
            )
            assert raised.value.operation == 3
            assert "at_new" not in await _tables(client)
            assert set(await _fields(client, "at_h")) == {"c"}
            assert await executor.get_applied_migrations() == []
            await _run(client, "REMOVE FIELD c ON at_h;")
            assert await executor.migrate() == [name]
            assert {"a", "b"} <= set(await _fields(client, "at_new"))
            assert "c" in await _fields(client, "at_h")

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_let_cannot_change_the_recorded_name(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind):
            name = _write(tmp_path, "let", [CreateTable("at_l", "SCHEMALESS"), RawSQL("LET $surreal_orm_migration = 'x';")])
            executor = MigrationExecutor(tmp_path)
            assert await executor.migrate() == [name]
            assert await executor.get_applied_migrations() == [name]


_AP_ANALYZER = "ap_en"


class ApDoc(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="ap_doc", schema_mode=SchemaMode.SCHEMAFULL)

    id: str | None = None
    title: str = ""


class TestAnalyzerPhaseE2E:
    """AC-10: an analyzer and the full-text index that uses it, in one migration, on a table holding records — the
    analyzer committed first (phase A, M4) —; a search finds a record saved before and one saved after."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_an_analyzer_and_its_index_on_a_table_holding_records(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind):
            first = _write(tmp_path, "docs", [CreateTable("ap_doc", "SCHEMAFULL"), AddField("ap_doc", "title", "string")])
            executor = MigrationExecutor(tmp_path)
            assert await executor.migrate() == [first]
            await ApDoc(id="before", title="Hello World").save()
            second = _write(
                tmp_path,
                "search",
                [
                    DefineAnalyzer(_AP_ANALYZER, ["blank", "class"], ["lowercase"]),
                    CreateIndex("ap_doc", "ap_title", ["title"], search_analyzer=_AP_ANALYZER, bm25=True),
                ],
                [first],
            )
            assert await executor.migrate() == [second]
            await ApDoc(id="after", title="hello again").save()
            found = await ApDoc.objects().search(title="hello").exec()
            assert sorted(doc.title for doc in found) == ["Hello World", "hello again"]


_CP_DEFINITION = DefineAnalyzer("cp_an", ["blank"], ["lowercase"])
_CO_ANALYZER = Analyzer("co_an", ["blank"], ["lowercase"])


class CoDoc(BaseSurrealModel):
    """A model whose full-text index and analyzer ``define_indexes()`` defines (no migration defines them)."""

    model_config = SurrealConfigDict(
        table_name="co_doc",
        schema_mode=SchemaMode.SCHEMAFULL,
        indexes=[Index("co_title", "title", search_analyzer=_CO_ANALYZER, bm25=True)],
    )

    id: str | None = None
    title: str = ""


async def _hello(model: type[BaseSurrealModel]) -> int:
    return len(await model.objects().search(title="hello").exec())


async def _unchanged(before: SchemaState) -> None:
    """The database's schema equals *before* for every clause, both ways (unmarked states, spec §5.7)."""
    after = await DatabaseIntrospector().introspect()
    assert before.diff(after) == []
    assert after.diff(before) == []


class TestCompensationE2E:
    """AC-9 (second part), AC-9 + (C8, D38): when phase B fails, phase A's analyzers are restored to the definitions
    the database held before ``migrate()`` — a migration's earlier definition, or ``define_indexes()``'s —, whatever
    ``previous=`` says; a new analyzer is removed; ``analyzers_restored`` says so."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_redefined_analyzer_is_restored(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind):
            first = _write(
                tmp_path,
                "docs",
                [
                    _CP_DEFINITION,
                    CreateTable("co_doc", "SCHEMAFULL"),
                    AddField("co_doc", "title", "string"),
                    CreateIndex("co_doc", "co_title", ["title"], search_analyzer="cp_an", bm25=True),
                ],
            )
            executor = MigrationExecutor(tmp_path)
            assert await executor.migrate() == [first]
            await CoDoc(id="one", title="Hello World").save()
            assert await _hello(CoDoc) == 1
            before = await DatabaseIntrospector().introspect()
            second = _write(
                tmp_path,
                "change",
                [
                    DefineAnalyzer("cp_an", ["blank"], ["uppercase"], previous=_CP_DEFINITION),
                    RebuildIndex("co_doc", "co_title"),
                    RawSQL("THROW 'boom'"),
                ],
                [first],
            )
            with pytest.raises(MigrationStatementError) as raised:
                await executor.migrate()
            assert str(raised.value) == (
                f"Migration '{second}' failed at operation 3 of 3 (Raw SQL): An error occurred: boom {_RESTORED}"
            )
            assert raised.value.analyzers_restored is True
            await _unchanged(before)
            assert await _hello(CoDoc) == 1
            assert await executor.get_applied_migrations() == [first]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_new_analyzer_is_removed(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind):
            name = _write(tmp_path, "new", [DefineAnalyzer("cp_new", ["blank"]), RawSQL("THROW 'boom'")])
            before = await DatabaseIntrospector().introspect()
            with pytest.raises(MigrationStatementError) as raised:
                await MigrationExecutor(tmp_path).migrate()
            assert (
                str(raised.value)
                == f"Migration '{name}' failed at operation 2 of 2 (Raw SQL): An error occurred: boom {_RESTORED}"
            )
            assert raised.value.analyzers_restored is True
            after = await DatabaseIntrospector().introspect()
            assert "cp_new" not in after.analyzers
            assert before.diff(after) == []

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_an_analyzer_define_indexes_made_is_restored(
        self, kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "dont_write_bytecode", True)  # the file is written again below
        async with migration_database(kind):
            await CoDoc.define_table()
            await CoDoc.define_indexes()
            await CoDoc(id="one", title="Hello World").save()
            before = await DatabaseIntrospector().introspect()
            assert "co_an" in before.analyzers
            executor = MigrationExecutor(tmp_path)
            unrelated = DefineAnalyzer("co_an", ["class"], ["uppercase"])
            for previous in (unrelated, None):
                operation = DefineAnalyzer("co_an", ["blank"], ["lowercase", "ascii"], previous=previous)
                name = _write(tmp_path, "change", [operation, RawSQL("THROW 'boom'")])
                with pytest.raises(MigrationStatementError) as raised:
                    await executor.migrate()
                assert raised.value.analyzers_restored is True, previous
                assert str(raised.value).endswith(_RESTORED)
                await _unchanged(before)
                assert await _hello(CoDoc) == 1
                (tmp_path / f"{name}.py").unlink()


class TestConcurrencyE2E:
    """AC-14 (spec §9.3): four executors on four connections to one database run ``migrate()`` at once: one record
    per migration, every definition once; each call returns the names it applied (maybe none) or raises the
    retryable ``SurrealDbConflictError`` — never a ``MigrationStatementError``."""

    async def test_four_executors_at_once(self, tmp_path: Path) -> None:
        database = _database_name()
        async with migration_database("ws", database) as client:
            first = _write(
                tmp_path,
                "a",
                [
                    CreateTable("cc_t", "SCHEMAFULL"),
                    AddField("cc_t", "a", "int"),
                    CreateIndex("cc_t", "cc_a", ["a"], unique=True),
                ],
            )
            second = _write(tmp_path, "b", [AddField("cc_t", "b", "string")], [first])
            names = [f"mig_c{index}" for index in range(4)]
            for name in names:
                SurrealDBConnectionManager.add_connection(
                    name, url=_url("ws"), user="root", password="root", namespace="ns", database=database
                )
            outcomes = await asyncio.gather(
                *(MigrationExecutor(tmp_path, connection=name).migrate() for name in names), return_exceptions=True
            )
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    assert isinstance(outcome, SurrealDbConflictError), repr(outcome)
                    assert not isinstance(outcome, MigrationStatementError)
                else:
                    assert outcome in ([], [first], [second], [first, second]), outcome
            applied = await MigrationExecutor(tmp_path).get_applied_migrations()
            assert applied == [first, second]
            fields = await _fields(client, "cc_t")
            assert set(fields) == {"a", "b"}


class CaItem(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="ca_item")

    id: str | None = None
    n: int = 0
    doubled: int | None = None


class TestCacheE2E:
    """AC-21: a cached read of a table a migration changes is not served after ``migrate()`` — every batch drops the
    connection's cached reads (``cache.writing(None)``)."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_cached_read_is_not_served_after_migrate(self, kind: str, tmp_path: Path) -> None:
        QueryCache.clear()
        async with migration_database(kind):
            await CaItem(id="one", n=2).save()
            first = await CaItem.objects().cache(ttl=60).exec()
            assert [item.doubled for item in first] == [None]
            hits = QueryCache.stats()["hits"]
            assert [item.doubled for item in await CaItem.objects().cache(ttl=60).exec()] == [None]
            assert QueryCache.stats()["hits"] == hits + 1
            name = _write(
                tmp_path, "doubled", [AddField("ca_item", "doubled", "any", value="n * 2"), RawSQL("UPDATE ca_item;")]
            )
            assert await MigrationExecutor(tmp_path).migrate() == [name]
            after = await CaItem.objects().cache(ttl=60).exec()
            assert [item.doubled for item in after] == [4]


_MARKER = "s3cr3t-marker"


class TestNoStatementLeakE2E:
    """AC-22: no error, warning or log of ``migrate()`` holds a statement's text — a marker in a ``VALUE``, a table
    permission rule and a refused ``RawSQL`` is absent from ``str()`` and ``repr()`` of the error and of the
    operations, and from every captured log record."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_the_marker_never_shows(self, kind: str, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG, logger="surreal_orm_lite")
        async with migration_database(kind):
            operations: list[Operation] = [
                CreateTable("nl_t", "SCHEMALESS", permissions={"select": f"WHERE name != '{_MARKER}'"}),
                AddField("nl_t", "name", "string", value=f"'{_MARKER}'"),
                RawSQL(f"DEFINE FIELD ON nl_t TYPE string ASSERT $value != '{_MARKER}'"),
            ]
            _write(tmp_path, "leak", operations)
            with pytest.raises(MigrationStatementError) as raised:
                await MigrationExecutor(tmp_path).migrate()
            assert raised.value.operation == 3
            assert "Parse error" in str(raised.value)
            for text in (str(raised.value), repr(raised.value), caplog.text, *map(repr, operations)):
                assert _MARKER not in text


# --- E2E: rollback(), upgrade(), the data operations, get_migration_status() -----------------------------------------

#: The engines of the rollback, data and status classes but ``TestRollbackE2E``: WebSocket, and the embedded engine.
_WS_KINDS = ["ws", "mem"]


def _rolled_back_logs(*names: str) -> list[str]:
    return [message for name in names for message in (f"Rolling back: {name}", f"Rolled back: {name}")]


class TestRollbackE2E:
    """AC-11, AC-18 (``rollback()``'s part): ``rollback(<first>)`` undoes the later migrations, newest first — their
    definitions and tracking records gone —, ``rollback("")`` every one; an irreversible migration, a target not
    applied, an applied name without a file and an applied migration whose dependency is pending refuse before any
    DDL; a definition removed by hand meanwhile still rolls back (``REMOVE … IF EXISTS``)."""

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_back_to_the_first_then_all(self, kind: str, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        async with migration_database(kind) as client:
            first = _write(tmp_path, "a", [CreateTable("rb_a", "SCHEMAFULL"), AddField("rb_a", "name", "string")])
            second = _write(
                tmp_path,
                "b",
                [CreateTable("rb_b", "SCHEMALESS"), AddField("rb_b", "n", "int"), CreateIndex("rb_b", "rb_n", ["n"])],
                [first],
            )
            third = _write(tmp_path, "c", [AddField("rb_a", "age", "int")], [second])
            executor = MigrationExecutor(tmp_path)
            assert await executor.migrate() == [first, second, third]
            caplog.set_level(logging.INFO, logger=_LOGGER)
            assert await executor.rollback(first) == [third, second]
            assert caplog.messages == _rolled_back_logs(third, second)
            assert await executor.get_applied_migrations() == [first]
            assert "rb_b" not in await _tables(client)
            assert set(await _fields(client, "rb_a")) == {"name"}
            caplog.clear()
            assert await executor.rollback(first) == []
            assert caplog.messages == ["No migrations to rollback."]
            assert await executor.rollback("") == [first]
            assert await executor.get_applied_migrations() == []
            assert not {"rb_a", "rb_b"} & await _tables(client)
            assert await executor.migrate() == [first, second, third]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_an_irreversible_migration_refuses_before_any_ddl(
        self, kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async with migration_database(kind) as client:
            first = _write(tmp_path, "a", [CreateTable("rb_x", "SCHEMALESS"), CreateTable("rb_y", "SCHEMALESS")])
            second = _write(tmp_path, "b", [AddField("rb_x", "n", "int"), DropTable("rb_y")], [first])
            third = _write(tmp_path, "c", [AddField("rb_x", "m", "int")], [second])
            executor = MigrationExecutor(tmp_path)
            assert await executor.migrate() == [first, second, third]
            before = await _info_db(client)
            fields = await _fields(client, "rb_x")
            reads = _read_spy(monkeypatch)
            with pytest.raises(ValueError) as raised:
                await executor.rollback("")
            assert str(raised.value) == (
                f"Migration {second} is not reversible: operation 2 (Drop table rb_y) cannot be rolled back."
            )
            assert reads == []
            assert await _info_db(client) == before
            assert await _fields(client, "rb_x") == fields
            assert await executor.get_applied_migrations() == [first, second, third]
            assert await executor.rollback(second) == [third]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_target_that_is_not_applied(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind):
            first = _write(tmp_path, "a", [CreateTable("rb_t", "SCHEMALESS")])
            second = _write(tmp_path, "b", [AddField("rb_t", "n", "int")], [first])
            executor = MigrationExecutor(tmp_path)
            assert await executor.migrate(target=first) == [first]
            for target in (second, "0009_x"):
                with pytest.raises(ValueError) as raised:
                    await executor.rollback(target)
                assert str(raised.value) == f"Cannot roll back to {target!r}: it is not applied."
            assert await executor.get_applied_migrations() == [first]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_an_applied_name_without_a_file_refuses_before_any_ddl(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind) as client:
            first = _write(tmp_path, "a", [CreateTable("rb_f", "SCHEMALESS")])
            executor = MigrationExecutor(tmp_path)
            assert await executor.migrate() == [first]
            await _record(client, "0099_gone")
            before = await _info_db(client)
            with pytest.raises(FileNotFoundError) as raised:
                await executor.rollback("")
            assert str(raised.value) == f"Migration file not found: {tmp_path / '0099_gone.py'}"
            assert await _info_db(client) == before
            assert await executor.get_applied_migrations() == [first, "0099_gone"]

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_an_applied_migration_whose_dependency_is_pending(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind) as client:
            first = _write(tmp_path, "a", [CreateTable("rb_d", "SCHEMALESS")])
            second = _write(tmp_path, "b", [CreateTable("rb_e", "SCHEMALESS")], [first])
            executor = MigrationExecutor(tmp_path)
            await executor.ensure_migrations_table()
            await _record(client, second)
            before = await _info_db(client)
            with pytest.raises(ValueError) as raised:
                await executor.rollback("")
            assert str(raised.value) == (
                f"Migration '{second}' is applied but its dependency '{first}' is not: the database's history does not "
                f"match {tmp_path}."
            )
            assert await _info_db(client) == before

    @pytest.mark.parametrize("kind", _KINDS)
    async def test_a_creation_removed_by_hand_still_rolls_back(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind) as client:
            first = _write(
                tmp_path,
                "a",
                [CreateTable("rb_h", "SCHEMALESS"), AddField("rb_h", "n", "int"), CreateIndex("rb_h", "rb_hn", ["n"])],
            )
            executor = MigrationExecutor(tmp_path)
            assert await executor.migrate() == [first]
            await _run(client, "REMOVE TABLE rb_h;")
            assert await executor.rollback("") == [first]
            assert await executor.get_applied_migrations() == []
            assert "rb_h" not in await _tables(client)


class RsLoose(BaseSurrealModel):
    """A SCHEMALESS model with an ``Any`` column: ``define_table()`` adds ``bag TYPE any`` to ``rs_t``, outside every
    migration."""

    model_config = SurrealConfigDict(table_name="rs_t", schema_mode=SchemaMode.SCHEMALESS)

    id: str | None = None
    bag: Any = None


def _send_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The text of every batch the executor sends; the real batch is sent."""
    texts: list[str] = []
    real = executor_module._send

    async def spy(client: Any, batch: "executor_module._Batch", line: int, name: str | None) -> Any:
        texts.append(batch.text())
        return await real(client, batch, line, name)

    monkeypatch.setattr(executor_module, "_send", spy)
    return texts


class TestRollbackStatesE2E:
    """C8, D35 (AC-11): a rollback reads the database's schema exactly once — never when nothing is to be undone —
    and each backwards renders from that read rewound, so a column ``define_table()`` added outside migrations is
    redefined ``FLEXIBLE`` below 3 when the table becomes ``SCHEMAFULL`` again."""

    @pytest.mark.parametrize("kind", _WS_KINDS)
    async def test_one_read_per_rollback(self, kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        async with migration_database(kind):
            first = _write(tmp_path, "a", [CreateTable("rs_a", "SCHEMALESS")])
            second = _write(tmp_path, "b", [AddField("rs_a", "n", "int")], [first])
            third = _write(tmp_path, "c", [CreateTable("rs_c", "SCHEMALESS")], [second])
            executor = MigrationExecutor(tmp_path)
            assert await executor.migrate() == [first, second, third]
            reads = _read_spy(monkeypatch)
            assert await executor.rollback("") == [third, second, first]
            assert len(reads) == 1
            assert await executor.rollback("") == []
            assert len(reads) == 1

    @pytest.mark.parametrize("kind", _WS_KINDS)
    async def test_the_backwards_render_from_the_read_rewound(
        self, kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async with migration_database(kind) as client:
            line = await _line(client)
            first = _write(tmp_path, "a", [CreateTable("rs_t", "SCHEMAFULL")])
            second = _write(tmp_path, "b", [AlterTable("rs_t", "SCHEMALESS", previous_schema_mode="SCHEMAFULL")], [first])
            executor = MigrationExecutor(tmp_path)
            assert await executor.migrate() == [first, second]
            await RsLoose.define_table()
            assert "FLEXIBLE" not in (await _fields(client, "rs_t"))["bag"]
            sent = _send_spy(monkeypatch)
            assert await executor.rollback(first) == [second]
            (batch,) = [text for text in sent if _UNRECORD in text]
            statements = _sent(batch)
            assert statements[:2] == [_UNRECORD, "DEFINE TABLE OVERWRITE `rs_t` SCHEMAFULL;"]
            redefined = [statement for statement in statements if statement.startswith("DEFINE FIELD")]
            fields = await _fields(client, "rs_t")
            if line < 3:
                assert redefined == ["DEFINE FIELD OVERWRITE `bag` ON `rs_t` TYPE any FLEXIBLE;"]
                assert "FLEXIBLE" in fields["bag"]
            else:
                assert redefined == []
                assert "FLEXIBLE" not in fields["bag"]
            assert "SCHEMAFULL" in (await _info_db(client))["tables"]["rs_t"]
            await _run(client, "UPSERT rs_t:one SET bag = { a: { b: 1 } };")
            assert await client.query("SELECT VALUE bag FROM rs_t:one;", {}) == [{"a": {"b": 1}}]


class RvDoc(BaseSurrealModel):
    """The full-text indexed table of ``TestReversibleE2E``'s analyzer cases (migrations define it)."""

    model_config = SurrealConfigDict(table_name="rv_doc", schema_mode=SchemaMode.SCHEMAFULL)

    id: str | None = None
    title: str = ""


_RV_ANALYZER = DefineAnalyzer("rv_an", ["blank"], ["lowercase"])
_RV_SEARCH = CreateIndex("rv_doc", "rv_title", ["title"], search_analyzer="rv_an", bm25=True)
_RV_INDEX = CreateIndex("rv_t", "rv_i", ["a"])
_RV_EVENT = DefineEvent("rv_e", "rv_t", None, "CREATE rv_log SET at = time::now()")
_RV_ACCESS = DefineAccess(
    "rv_acc",
    "rv_user",
    {"email": "$email", "password": "crypto::argon2::generate($password)"},
    "email = $email AND crypto::argon2::compare(password, $password)",
)
#: Each case: the first migration (what the second changes), the second, and whether a search on ``rv_doc`` runs.
_REVERSIBLE: dict[str, tuple[list[Operation], list[Operation], bool]] = {
    "alter_field": (
        [CreateTable("rv_t", "SCHEMAFULL"), AddField("rv_t", "a", "int", 1, "$value > 0")],
        [
            AlterField(
                "rv_t", "a", "int", 2, "$value > 1", previous_type="int", previous_default=1, previous_assertion="$value > 0"
            )
        ],
        False,
    ),
    "alter_table": (
        [CreateTable("rv_t", "SCHEMAFULL", comment="one")],
        [
            AlterTable(
                "rv_t",
                "SCHEMALESS",
                "normal",
                comment="two",
                previous_schema_mode="SCHEMAFULL",
                previous_comment="one",
                drop=True,
            )
        ],
        False,
    ),
    "create_index": (
        [CreateTable("rv_t", "SCHEMAFULL"), AddField("rv_t", "a", "int"), _RV_INDEX],
        [CreateIndex("rv_t", "rv_i", ["a"], unique=True, previous=_RV_INDEX)],
        False,
    ),
    "define_event": (
        [CreateTable("rv_t", "SCHEMALESS"), _RV_EVENT],
        [DefineEvent("rv_e", "rv_t", "$event = 'CREATE'", "CREATE rv_log SET at = time::now(), k = 1", previous=_RV_EVENT)],
        False,
    ),
    "define_analyzer": (
        [_RV_ANALYZER, CreateTable("rv_doc", "SCHEMAFULL"), AddField("rv_doc", "title", "string"), _RV_SEARCH],
        [DefineAnalyzer("rv_an", ["blank"], ["lowercase", "ascii"], previous=_RV_ANALYZER), RebuildIndex("rv_doc", "rv_title")],
        True,
    ),
    "define_access": (
        [
            CreateTable("rv_user", "SCHEMAFULL"),
            AddField("rv_user", "email", "string"),
            AddField("rv_user", "password", "string"),
            _RV_ACCESS,
        ],
        [
            DefineAccess(
                "rv_acc",
                "rv_user",
                {"email": "$email", "password": "crypto::argon2::generate($password)"},
                "email = $email AND crypto::argon2::compare(password, $password)",
                "30m",
                "1h",
                previous=_RV_ACCESS,
            )
        ],
        False,
    ),
    "drop_index": (
        [CreateTable("rv_t", "SCHEMAFULL"), AddField("rv_t", "a", "int"), _RV_INDEX],
        [DropIndex("rv_t", "rv_i", previous=_RV_INDEX)],
        False,
    ),
    "remove_analyzer": (
        [_RV_ANALYZER, CreateTable("rv_doc", "SCHEMAFULL"), AddField("rv_doc", "title", "string"), _RV_SEARCH],
        [DropIndex("rv_doc", "rv_title", previous=_RV_SEARCH), RemoveAnalyzer("rv_an", previous=_RV_ANALYZER)],
        True,
    ),
    "remove_access": (
        [
            CreateTable("rv_user", "SCHEMAFULL"),
            AddField("rv_user", "email", "string"),
            AddField("rv_user", "password", "string"),
            _RV_ACCESS,
        ],
        [RemoveAccess("rv_acc", previous=_RV_ACCESS)],
        False,
    ),
}


class TestReversibleE2E:
    """AC-12: a reversible redefinition (or a removal with ``previous=``) migrated then rolled back leaves the
    database's schema as it was — ``diff()`` between the reads before and after is ``[]`` both ways, every clause
    compared (unmarked states) —, while the read right after ``migrate()`` differs from it; a search on a full-text
    index whose analyzer the migration changed or removed matches again after the rollback."""

    @pytest.mark.parametrize("case", list(_REVERSIBLE))
    @pytest.mark.parametrize("kind", _WS_KINDS)
    async def test_migrate_then_rollback_restores_the_schema(self, kind: str, case: str, tmp_path: Path) -> None:
        setup, change, searched = _REVERSIBLE[case]
        async with migration_database(kind):
            first = _write(tmp_path, "setup", setup)
            second = _write(tmp_path, "change", change, [first])
            executor = MigrationExecutor(tmp_path)
            assert await executor.migrate(target=first) == [first]
            if searched:
                await RvDoc(id="one", title="Hello World").save()
                assert await _hello(RvDoc) == 1
            before = await DatabaseIntrospector().introspect()
            assert list(before.issues) == []
            assert await executor.migrate() == [second]
            changed = await DatabaseIntrospector().introspect()
            assert before.diff(changed) != []
            assert await executor.rollback(first) == [second]
            await _unchanged(before)
            if searched:
                assert await _hello(RvDoc) == 1
            assert await executor.get_applied_migrations() == [first]


_RC_ANALYZER = DefineAnalyzer("rc_an", ["blank"], ["lowercase"])


class RcDoc(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="rc_doc", schema_mode=SchemaMode.SCHEMAFULL)

    id: str | None = None
    title: str = ""


class TestRollbackCompensationE2E:
    """AC-9 (rollback part), D38: a rollback whose phase B is refused — its table altered by hand so that the
    ``AlterField`` backwards is refused — restores the analyzer definitions its phase A committed to the ones the
    database held before the rollback (the migration's own), and says so (E-X3)."""

    @pytest.mark.parametrize("kind", _WS_KINDS)
    async def test_the_analyzer_is_restored_to_the_databases_definition(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind) as client:
            first = _write(
                tmp_path,
                "docs",
                [
                    _RC_ANALYZER,
                    CreateTable("rc_doc", "SCHEMAFULL"),
                    AddField("rc_doc", "title", "string"),
                    CreateIndex("rc_doc", "rc_title", ["title"], search_analyzer="rc_an", bm25=True),
                    CreateTable("rc_t", "SCHEMALESS"),
                    AddField("rc_t", "in", "int"),
                ],
            )
            second = _write(
                tmp_path,
                "change",
                [
                    DefineAnalyzer("rc_an", ["blank"], ["lowercase", "ascii"], previous=_RC_ANALYZER),
                    RebuildIndex("rc_doc", "rc_title"),
                    AlterField("rc_t", "in", "record<rc_a>", previous_type="int"),
                ],
                [first],
            )
            executor = MigrationExecutor(tmp_path)
            assert await executor.migrate() == [first, second]
            await RcDoc(id="one", title="Hello World").save()
            # By hand: rc_t becomes a relation, whose `in` must be a record — the AlterField's backwards is refused.
            await _run(client, "DEFINE TABLE OVERWRITE rc_t TYPE RELATION IN rc_a OUT rc_b;")
            before = await DatabaseIntrospector().introspect()
            with pytest.raises(MigrationStatementError) as raised:
                await executor.rollback(first)
            assert str(raised.value) == (
                f"Rollback of migration '{second}' failed at operation 3 of 3 (Alter field in on rc_t): An error "
                f"occurred: in field on a relation must be a record {_RESTORED}"
            )
            assert (raised.value.operation, raised.value.analyzers_restored) == (3, True)
            await _unchanged(before)
            assert await _hello(RcDoc) == 1
            assert await executor.get_applied_migrations() == [first, second]


_EF_EVENT = DefineEvent("ef_e", "ef_t", None, "CREATE ef_log SET k = 1")
_EF_EVENT_CHANGED = DefineEvent("ef_e", "ef_t", "$event = 'CREATE'", "CREATE ef_log SET k = 2")
_EF_FUNCTION = DefineFunction("fn::ef_greet", "RETURN 'hi ' + $n;", parameters={"n": "string"}, returns="string")
_EF_FUNCTION_CHANGED = DefineFunction("fn::ef_greet", "RETURN 'hello ' + $n;", parameters={"n": "string"}, returns="string")


class TestEventsAndFunctionsE2E:
    """AC-27 (#276): an event and a function added, then changed (``previous=``), then removed
    (``RemoveEvent``/``RemoveFunction(previous=…)``) by three migrations; ``rollback()`` one step at a time restores
    each earlier state, the function's definition included (which the diff does not compare)."""

    @pytest.mark.parametrize("kind", _WS_KINDS)
    async def test_three_steps_rolled_back_one_by_one(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind) as client:
            first = _write(tmp_path, "add", [CreateTable("ef_t", "SCHEMALESS"), _EF_EVENT, _EF_FUNCTION])
            changes = [
                dataclasses.replace(_EF_EVENT_CHANGED, previous=_EF_EVENT),
                dataclasses.replace(_EF_FUNCTION_CHANGED, previous=_EF_FUNCTION),
            ]
            second = _write(tmp_path, "change", changes, [first])
            third = _write(
                tmp_path,
                "remove",
                [
                    RemoveEvent("ef_e", "ef_t", previous=_EF_EVENT_CHANGED),
                    RemoveFunction("fn::ef_greet", previous=_EF_FUNCTION_CHANGED),
                ],
                [second],
            )
            executor = MigrationExecutor(tmp_path)
            reads: list[SchemaState] = [await DatabaseIntrospector().introspect()]
            for name in (first, second, third):
                assert await executor.migrate(target=name) == [name]
                reads.append(await DatabaseIntrospector().introspect())
            assert reads[1].functions != reads[2].functions
            assert "ef_e" not in reads[3].tables["ef_t"].events
            assert reads[3].functions == {}
            greetings = {second: "hello x", first: "hi x"}
            for target, expected, undone in ((second, reads[2], third), (first, reads[1], second), ("", reads[0], first)):
                assert await executor.rollback(target) == [undone]
                now = await DatabaseIntrospector().introspect()
                assert now.diff(expected) == []
                assert expected.diff(now) == []
                assert now.functions == expected.functions
                if target:
                    assert await client.query("RETURN fn::ef_greet('x');", {}) == greetings[target]


#: A hand-written migration file: ``AddField`` then a ``DataMigration`` whose functions (which a generated file cannot
#: hold) record what its SQL wrote — ``seen = a`` after ``a = n + 1``, ``back_seen = back`` after ``back = 10`` — so
#: the record shows that each function ran after its SQL.
_DATA_FILE = """from surreal_orm_lite import SurrealDBConnectionManager
from surreal_orm_lite.migrations import Migration
from surreal_orm_lite.migrations.operations import AddField, DataMigration


async def forwards() -> None:
    client = await SurrealDBConnectionManager.get_client()
    await client.query("UPDATE dm_item SET seen = a;", {{}})


async def backwards() -> None:
    client = await SurrealDBConnectionManager.get_client()
    await client.query("UPDATE dm_item SET back_seen = back;", {{}})


migration = Migration(
    name={name!r},
    dependencies={dependencies!r},
    operations=[
        AddField("dm_item", "a", "int", nullable=True),
        DataMigration(
            forwards_sql={forwards_sql!r},
            backwards_sql="UPDATE dm_item SET back = 10;",
            forwards_func=forwards,
            backwards_func=backwards,
        ),
    ],
)
"""


def _data_files(directory: Path, *, forwards_sql: str = "UPDATE dm_item SET a = n + 1;") -> tuple[str, str]:
    """``0001_items`` (the table ``dm_item``) and the hand-written ``0002_data`` of :data:`_DATA_FILE`."""
    first = _write(directory, "items", [CreateTable("dm_item", "SCHEMALESS")])
    second = "0002_data"
    text = _DATA_FILE.format(name=second, dependencies=[first], forwards_sql=forwards_sql)
    (directory / f"{second}.py").write_text(text, encoding="utf-8")
    return first, second


async def _item(client: Any) -> dict[str, Any]:
    rows = await client.query("SELECT * OMIT id FROM dm_item:one;", {})
    assert isinstance(rows, list) and len(rows) == 1, rows
    return dict(rows[0])


class TestDataE2E:
    """AC-15, AC-18 (``upgrade()``'s part): a ``DataMigration`` is not run by ``migrate()``; ``upgrade()`` runs its SQL
    (one batch per migration) then its function and returns the name; ``migrate(schema_only=False)`` runs them after
    the schema (phase C); ``rollback()`` runs ``backwards_sql`` then ``backwards_func``; a refused data SQL raises E-X5
    and the migration stays recorded; ``upgrade()``'s history checks run before any data operation."""

    @pytest.mark.parametrize("kind", _WS_KINDS)
    async def test_migrate_upgrade_then_rollback(self, kind: str, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        async with migration_database(kind) as client:
            first, second = _data_files(tmp_path)
            executor = MigrationExecutor(tmp_path)
            assert await executor.migrate(target=first) == [first]
            await _run(client, "CREATE dm_item:one SET n = 1;")
            assert await executor.migrate() == [second]
            assert await _item(client) == {"n": 1}
            assert "a" in await _fields(client, "dm_item")
            caplog.set_level(logging.INFO, logger=_LOGGER)
            assert await executor.upgrade() == [second]
            assert caplog.messages == [f"Running data migrations for: {second}"]
            assert await _item(client) == {"n": 1, "a": 2, "seen": 2}
            assert await executor.upgrade(target=first) == []
            assert await executor.rollback(first) == [second]
            assert await _item(client) == {"n": 1, "a": 2, "seen": 2, "back": 10, "back_seen": 10}
            assert "a" not in await _fields(client, "dm_item")
            assert await executor.get_applied_migrations() == [first]
            assert await executor.upgrade() == []

    @pytest.mark.parametrize("kind", _WS_KINDS)
    async def test_migrate_runs_the_data_after_the_schema(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind) as client:
            first, second = _data_files(tmp_path)
            executor = MigrationExecutor(tmp_path)
            assert await executor.migrate(target=first, schema_only=False) == [first]
            await _run(client, "CREATE dm_item:one SET n = 1;")
            assert await executor.migrate(schema_only=False) == [second]
            assert await _item(client) == {"n": 1, "a": 2, "seen": 2}
            assert await executor.get_applied_migrations() == [first, second]

    @pytest.mark.parametrize("kind", _WS_KINDS)
    async def test_a_refused_data_sql_raises_and_stays_recorded(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind) as client:
            first, second = _data_files(tmp_path, forwards_sql="THROW 'data';")
            executor = MigrationExecutor(tmp_path)
            assert await executor.migrate(target=first) == [first]
            await _run(client, "CREATE dm_item:one SET n = 1;")
            expected = f"Data migration of '{second}' failed at operation 2 of 2 (Data migration): An error occurred: data"
            with pytest.raises(MigrationStatementError) as raised:
                await executor.migrate(schema_only=False)
            assert str(raised.value) == expected
            assert (raised.value.migration, raised.value.operation, raised.value.description) == (
                second,
                2,
                "Data migration",
            )
            assert await executor.get_applied_migrations() == [first, second]
            assert "a" in await _fields(client, "dm_item")
            assert await _item(client) == {"n": 1}
            with pytest.raises(MigrationStatementError) as again:
                await executor.upgrade()
            assert str(again.value) == expected
            assert await _item(client) == {"n": 1}

    @pytest.mark.parametrize("kind", _WS_KINDS)
    async def test_upgrade_with_an_applied_name_without_a_file(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind) as client:
            first, second = _data_files(tmp_path)
            executor = MigrationExecutor(tmp_path)
            assert await executor.migrate() == [first, second]
            await _run(client, "CREATE dm_item:one SET n = 1;")
            await _record(client, "0099_gone")
            with pytest.raises(FileNotFoundError) as raised:
                await executor.upgrade()
            assert str(raised.value) == f"Migration file not found: {tmp_path / '0099_gone.py'}"
            assert await _item(client) == {"n": 1}

    @pytest.mark.parametrize("kind", _WS_KINDS)
    async def test_upgrade_with_an_applied_migration_whose_dependency_is_pending(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind) as client:
            first, second = _data_files(tmp_path)
            executor = MigrationExecutor(tmp_path)
            await executor.ensure_migrations_table()
            await _run(client, "DEFINE TABLE dm_item SCHEMALESS; CREATE dm_item:one SET n = 1;")
            await _record(client, second)
            with pytest.raises(ValueError) as raised:
                await executor.upgrade()
            assert str(raised.value) == (
                f"Migration '{second}' is applied but its dependency '{first}' is not: the database's history does not "
                f"match {tmp_path}."
            )
            assert await _item(client) == {"n": 1}

    async def test_the_functions_run_on_the_executors_connection(self, tmp_path: Path) -> None:
        """The executor's named connection is the one its data functions talk to (``using()``), not the default."""
        other = _database_name()
        async with migration_database("ws") as client:
            await client.query(f"DEFINE DATABASE {other};", {})
            try:
                SurrealDBConnectionManager.add_connection(
                    "dm_other", url=_url("ws"), user="root", password="root", namespace="ns", database=other
                )
                first, second = _data_files(tmp_path)
                executor = MigrationExecutor(tmp_path, connection="dm_other")
                assert await executor.migrate(target=first) == [first]
                named = await SurrealDBConnectionManager.get_client("dm_other")
                await _run(named, "CREATE dm_item:one SET n = 1;")
                assert await executor.migrate(schema_only=False) == [second]
                assert await _item(named) == {"n": 1, "a": 2, "seen": 2}
                assert "dm_item" not in await _tables(client)
            finally:
                await client.query(f"REMOVE DATABASE IF EXISTS {other};", {})


class TestStatusE2E:
    """AC-13, AC-18 (``get_migration_status()``'s part): the full ORM's dict for every file, in application order,
    before and after ``migrate()``; it never raises on the history — an applied name without a file is warned about
    and not listed, an applied migration whose dependency is pending gives E-H1's text as a warning —; no schema
    read."""

    @pytest.mark.parametrize("kind", _WS_KINDS)
    async def test_the_status_before_and_after(self, kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        async with migration_database(kind):
            first, second = _data_files(tmp_path)
            third = _write(tmp_path, "drop", [CreateTable("st_x", "SCHEMALESS"), DropTable("st_x")], [second])
            executor = MigrationExecutor(tmp_path)
            reads = _read_spy(monkeypatch)
            status = await executor.get_migration_status()
            assert list(status) == [first, second, third]
            assert status == {
                first: {"applied": False, "reversible": True, "has_data": False, "operations": 1},
                second: {"applied": False, "reversible": True, "has_data": True, "operations": 2},
                third: {"applied": False, "reversible": False, "has_data": False, "operations": 2},
            }
            assert reads == []
            assert await executor.migrate(target=second) == [first, second]
            reads.clear()
            status = await executor.get_migration_status()
            assert [entry["applied"] for entry in status.values()] == [True, True, False]
            assert reads == []

    @pytest.mark.parametrize("kind", _WS_KINDS)
    async def test_an_applied_name_without_a_file_is_not_listed(
        self, kind: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        async with migration_database(kind) as client:
            first = _write(tmp_path, "a", [CreateTable("st_a", "SCHEMALESS")])
            executor = MigrationExecutor(tmp_path)
            assert await executor.migrate() == [first]
            await _record(client, "0099_gone")
            caplog.set_level(logging.WARNING, logger=_LOGGER)
            status = await executor.get_migration_status()
            assert status == {first: {"applied": True, "reversible": True, "has_data": False, "operations": 1}}
            warning = f"Migration 0099_gone is recorded as applied but has no file in {tmp_path}; it is ignored."
            assert caplog.messages == [warning]

    @pytest.mark.parametrize("kind", _WS_KINDS)
    async def test_an_applied_migration_whose_dependency_is_pending_is_warned(
        self, kind: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        async with migration_database(kind) as client:
            first = _write(tmp_path, "a", [CreateTable("st_d", "SCHEMALESS")])
            second = _write(tmp_path, "b", [CreateTable("st_e", "SCHEMALESS")], [first])
            executor = MigrationExecutor(tmp_path)
            await executor.ensure_migrations_table()
            await _record(client, second)
            caplog.set_level(logging.WARNING, logger=_LOGGER)
            status = await executor.get_migration_status()
            assert [entry["applied"] for entry in status.values()] == [False, True]
            assert caplog.messages == [
                f"Migration '{second}' is applied but its dependency '{first}' is not: the database's history does not "
                f"match {tmp_path}."
            ]


# === the pre-pass's refusals: line-only clauses, state guards, read issues (spec §5.6 step 3, §7) ======================

#: E-D1, E-D2, E-D3: what SurrealDB 2.x and the embedded engine cannot parse (spec §7).
_ASYNC_EVENTS = "ASYNC events"
_REFRESH_TOKENS = "refresh tokens (WITH REFRESH)"
_COMPUTED_FIELDS = "COMPUTED fields"
_ASYNC_EVENT = DefineEvent("audit", "doc", None, "CREATE log SET a = 1", async_mode=True)
_REFRESH_ACCESS = DefineAccess("acc", "users", {"email": "$email"}, "email = $email", with_refresh=True)


def _e_d(name: str, position: int, operation: Operation, needs: str) -> str:
    """E-D1/E-D2/E-D3 (spec §7)."""
    return (
        f"Migration '{name}', operation {position} ({operation.describe()}): {needs} need SurrealDB 3.x; this "
        "connection runs SurrealDB 2.x or the embedded engine."
    )


def _e_g1(name: str, position: int, operation: Operation, index: str, table: str) -> str:
    return (
        f"Migration '{name}', operation {position} ({operation.describe()}): the analyzer is still used by index "
        f"'{index}' on table '{table}'; drop the index first."
    )


def _e_g2(name: str, position: int, operation: Operation, view: str) -> str:
    return (
        f"Migration '{name}', operation {position} ({operation.describe()}): the view '{view}' reads this table; drop "
        "the view first."
    )


def _e_i1(name: str, position: int, operation: Operation, table: str) -> str:
    return (
        f"Migration '{name}', operation {position} ({operation.describe()}): table '{table}' could not be read entirely "
        "from the database, and on SurrealDB 2.x and the embedded engine the FLEXIBLE clauses of this operation are "
        "chosen from that reading; nothing was applied. DatabaseIntrospector().introspect().issues lists what could not "
        "be read."
    )


def _line_of(version: str) -> int:
    return 3 if version.startswith("surrealdb-3") else 2


_VERSIONS = ["surrealdb-2.7.0", "surrealdb-3.3.4"]


class TestLineRefusalsUnit:
    """Spec §5.6 step 3, §7 E-D1–E-D3, D27: below SurrealDB 3 a migration defining an ``ASYNC`` event, a ``WITH
    REFRESH`` access or a ``COMPUTED`` field is refused before any DDL — and so is a rollback whose backwards would
    define one —; SurrealDB 3.x applies it."""

    @pytest.mark.parametrize(
        ("operation", "needs"),
        [
            (_ASYNC_EVENT, _ASYNC_EVENTS),
            (_REFRESH_ACCESS, _REFRESH_TOKENS),
            (AddField("doc", "c", "int", computed="a + 1"), _COMPUTED_FIELDS),
            (AlterField("doc", "c", "int", computed="a + 1", previous_type="int"), _COMPUTED_FIELDS),
        ],
        ids=["async", "refresh", "add-computed", "alter-computed"],
    )
    @pytest.mark.parametrize("version", _VERSIONS)
    async def test_a_line_only_clause(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, operation: Operation, needs: str, version: str
    ) -> None:
        name = _write(tmp_path, "a", [CreateTable("doc", "SCHEMALESS"), operation])
        client = _FakeClient([_ok(3, line=_line_of(version))], version=version)
        _offline(monkeypatch, client, SchemaState())
        executor = MigrationExecutor(tmp_path)
        if _line_of(version) == 3:
            assert await executor.migrate() == [name]
            assert len(client.sent) == 1
            return
        with pytest.raises(SurrealDbError) as raised:
            await executor.migrate()
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == _e_d(name, 2, operation, needs)
        assert client.sent == []

    async def test_the_first_refusal_of_the_plan_is_raised(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        first = _write(tmp_path, "a", [CreateTable("doc", "SCHEMALESS")])
        second = _write(tmp_path, "b", [AddField("doc", "a", "int"), _REFRESH_ACCESS, _ASYNC_EVENT], [first])
        client = _FakeClient([], version="surrealdb-2.7.0")
        _offline(monkeypatch, client, SchemaState())
        with pytest.raises(SurrealDbError) as raised:
            await MigrationExecutor(tmp_path).migrate()
        assert str(raised.value) == _e_d(second, 2, _REFRESH_ACCESS, _REFRESH_TOKENS)
        assert client.sent == []

    @pytest.mark.parametrize(
        ("operation", "needs"),
        [
            (DefineEvent("audit", "doc", None, "CREATE log SET a = 2", previous=_ASYNC_EVENT), _ASYNC_EVENTS),
            (RemoveEvent("audit", "doc", previous=_ASYNC_EVENT), _ASYNC_EVENTS),
            (DefineAccess("acc", "users", {"email": "$email"}, "email = $email", previous=_REFRESH_ACCESS), _REFRESH_TOKENS),
            (RemoveAccess("acc", previous=_REFRESH_ACCESS), _REFRESH_TOKENS),
            (AlterField("doc", "c", "int", previous_type="int", previous_computed="a + 1"), _COMPUTED_FIELDS),
        ],
        ids=["define-event", "remove-event", "define-access", "remove-access", "alter-field"],
    )
    @pytest.mark.parametrize("version", _VERSIONS)
    async def test_a_backwards_line_only_clause(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, operation: Operation, needs: str, version: str
    ) -> None:
        name = _write(tmp_path, "a", [CreateTable("doc", "SCHEMALESS"), operation])
        client = _recording([_ok(3, line=_line_of(version))], name, version=version)
        _offline(monkeypatch, client, SchemaState())
        executor = MigrationExecutor(tmp_path)
        if _line_of(version) == 3:
            assert await executor.rollback("") == [name]
            assert len(client.sent) == 1
            return
        with pytest.raises(SurrealDbError) as raised:
            await executor.rollback("")
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == _e_d(name, 2, operation, needs)
        assert client.sent == []

    @pytest.mark.parametrize(
        "operation",
        [
            DefineEvent("audit", "doc", None, "CREATE log SET a = 1"),
            DefineEvent(
                "audit", "doc", None, "CREATE log SET a = 2", previous=DefineEvent("audit", "doc", None, "CREATE log SET a = 0")
            ),
            AlterField("doc", "c", "int", previous_type="string"),
        ],
        ids=["event", "event-previous", "alter-field"],
    )
    async def test_a_backwards_without_a_line_only_clause(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, operation: Operation
    ) -> None:
        name = _write(tmp_path, "a", [CreateTable("doc", "SCHEMALESS"), operation])
        client = _recording([_ok(3, line=2)], name, version="surrealdb-2.7.0")
        _offline(monkeypatch, client, SchemaState())
        assert await MigrationExecutor(tmp_path).rollback("") == [name]

    async def test_a_fake_run_refuses_nothing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        name = _write(tmp_path, "a", [_ASYNC_EVENT])
        client = _FakeClient([_ok(1, line=2)], version="surrealdb-2.7.0")
        _offline(monkeypatch, client, SchemaState())
        assert await MigrationExecutor(tmp_path).migrate(fake=True) == [name]
        assert _sent(client.sent[0]) == [_TRACKING]


def _read_view() -> SchemaState:
    """A read holding ``src``, ``other`` and the view ``v`` reading ``src``."""
    tables = {
        "src": TableState("src", "SCHEMALESS", "normal"),
        "other": TableState("other", "SCHEMALESS", "normal"),
        "v": TableState("v", "SCHEMALESS", "any", view_query="SELECT count() AS n FROM `src` GROUP ALL"),
    }
    return SchemaState(tables)


class TestGuardsUnit:
    """Spec §5.6, §7 E-G1/E-G2, D13 (AC-17): on the state before each operation — the read with the plan replayed, or
    for a rollback the read rewound through each backwards — the removal of an analyzer an index still uses and of a
    table a view reads are refused before any DDL, on every line; dropped first, the index or the view no longer
    counts."""

    @pytest.mark.parametrize("version", _VERSIONS)
    async def test_an_analyzer_in_use(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, version: str) -> None:
        removal = RemoveAnalyzer("english")
        name = _write(tmp_path, "a", [CreateTable("x", "SCHEMALESS"), removal])
        client = _FakeClient([], version=version)
        _offline(monkeypatch, client, _read_with_index())
        with pytest.raises(ValueError) as raised:
            await MigrationExecutor(tmp_path).migrate()
        # The first index (by table, then name) that uses it.
        assert str(raised.value) == _e_g1(name, 2, removal, "doc_title", "doc")
        assert client.sent == []

    async def test_an_analyzer_whose_indexes_were_dropped_first(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        operations: list[Operation] = [DropIndex("doc", "doc_title"), DropIndex("doc", "odd-name")]
        first = _write(tmp_path, "a", operations)
        second = _write(tmp_path, "b", [RemoveAnalyzer("english")], [first])
        client = _FakeClient([_ok(3), _ok(2)])
        _offline(monkeypatch, client, _read_with_index())
        assert await MigrationExecutor(tmp_path).migrate() == [first, second]

    async def test_an_analyzer_an_earlier_operation_puts_in_use(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        index = CreateIndex("doc", "doc_body", ["body"], search_analyzer="french", bm25=True)
        removal = RemoveAnalyzer("french")
        name = _write(tmp_path, "a", [DropIndex("doc", "doc_other"), index, removal])
        client = _FakeClient([])
        _offline(monkeypatch, client, _read_with_index())
        with pytest.raises(ValueError) as raised:
            await MigrationExecutor(tmp_path).migrate()
        assert str(raised.value) == _e_g1(name, 3, removal, "doc_body", "doc")

    @pytest.mark.parametrize("version", _VERSIONS)
    async def test_a_views_source(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, version: str) -> None:
        drop = DropTable("src")
        name = _write(tmp_path, "a", [DropTable("other"), drop])
        client = _FakeClient([], version=version)
        _offline(monkeypatch, client, _read_view())
        with pytest.raises(ValueError) as raised:
            await MigrationExecutor(tmp_path).migrate()
        assert str(raised.value) == _e_g2(name, 2, drop, "v")
        assert client.sent == []

    async def test_a_views_source_once_the_view_is_dropped(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        name = _write(tmp_path, "a", [DropTable("v"), DropTable("src")])
        client = _FakeClient([_ok(3)])
        _offline(monkeypatch, client, _read_view())
        assert await MigrationExecutor(tmp_path).migrate() == [name]

    async def test_a_rollback_removing_a_views_source(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        creation = CreateTable("src", "SCHEMALESS")
        name = _write(tmp_path, "a", [creation])
        client = _recording([], name)
        _offline(monkeypatch, client, _read_view())
        with pytest.raises(ValueError) as raised:
            await MigrationExecutor(tmp_path).rollback("")
        assert str(raised.value) == _e_g2(name, 1, creation, "v")
        assert client.sent == []

    async def test_a_rollback_removing_an_analyzer_in_use(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        definition = DefineAnalyzer("english", ["blank"])
        name = _write(tmp_path, "a", [definition, CreateTable("x", "SCHEMALESS")])
        client = _recording([], name)
        _offline(monkeypatch, client, _read_with_index())
        with pytest.raises(ValueError) as raised:
            await MigrationExecutor(tmp_path).rollback("")
        assert str(raised.value) == _e_g1(name, 1, definition, "doc_title", "doc")
        assert client.sent == []

    async def test_a_rollback_whose_backwards_drop_the_index_first(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Undone in reverse order: the index goes before its analyzer (M6), which is then no longer in use.
        operations: list[Operation] = [
            DefineAnalyzer("german", ["blank"]),
            CreateIndex("doc", "doc_de", ["body"], search_analyzer="german", bm25=True),
        ]
        name = _write(tmp_path, "a", operations)
        read = _read_with_index()
        doc = read.tables["doc"]
        index = IndexState("doc_de", ("body",), search_analyzer="german", bm25=True, table="doc")
        read = dataclasses.replace(
            read,
            tables={"doc": dataclasses.replace(doc, indexes={**doc.indexes, "doc_de": index})},
            analyzers={**read.analyzers, "german": AnalyzerState("german", ("blank",))},
        )
        client = _recording([_ok(3)], name)
        _offline(monkeypatch, client, read)
        assert await MigrationExecutor(tmp_path).rollback("") == [name]


#: A read issue on ``t``'s column ``lost``, on ``t``'s own definition, and the ``tables`` section issue.
_FIELD_ISSUE = SchemaIssue("field", "lost", "t", "could not parse its definition")
_TABLE_ISSUE = SchemaIssue("table", "t", "t", "INFO FOR TABLE failed")
_TABLES_ISSUE = SchemaIssue("section", "tables", None, "section 'tables' is not a mapping of names to definitions")


def _read_t_with(*issues: SchemaIssue) -> SchemaState:
    """``_read_t()``'s table ``t``, made ``SCHEMAFULL``, with *issues*."""
    read = _read_t()
    table = dataclasses.replace(read.tables["t"], schema_mode="SCHEMAFULL")
    return dataclasses.replace(read, tables={"t": table}, issues=issues)


class TestReadIssuesUnit:
    """Spec §5.6 step 3 and ``rollback()``, §7 E-I1 (C8, P2): on SurrealDB 2.x and the embedded engine, a ``FLEXIBLE``
    choice made from a table the read could not read entirely is refused before any DDL — (a) an ``AlterTable`` making
    it ``SCHEMAFULL`` (or a backwards restoring it) under an issue of that table or the ``tables`` section issue; (b) an
    ``AddField``/``AlterField`` of an ``any`` or object-array kind under the table's own issue or the ``tables`` section
    issue, unless an operation before it in the plan defines the table —; SurrealDB 3.x applies them."""

    @pytest.mark.parametrize("issue", [_FIELD_ISSUE, _TABLE_ISSUE, _TABLES_ISSUE], ids=["field", "table", "tables"])
    @pytest.mark.parametrize("version", _VERSIONS)
    async def test_a_table_made_schemafull(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, issue: SchemaIssue, version: str
    ) -> None:
        name = _write(tmp_path, "schemafull", [R33])
        line = _line_of(version)
        client = _FakeClient([_ok(1 + (len(_R33_LINES) if line == 2 else 1), line=line)], version=version)
        _offline(monkeypatch, client, dataclasses.replace(_read_t(), issues=(issue,)))
        executor = MigrationExecutor(tmp_path)
        if line == 3:
            assert await executor.migrate() == [name]
            return
        with pytest.raises(SurrealDbError) as raised:
            await executor.migrate()
        assert type(raised.value) is SurrealDbError
        assert str(raised.value) == _e_i1(name, 1, R33, "t")
        assert client.sent == []

    @pytest.mark.parametrize(
        "issue",
        [SchemaIssue("field", "x", "other", "?"), SchemaIssue("table", "other", "other", "?")],
        ids=["field", "table"],
    )
    async def test_an_issue_on_another_table(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, issue: SchemaIssue) -> None:
        name = _write(tmp_path, "schemafull", [R33, AddField("t", "box", "any")])
        client = _FakeClient([_ok(1 + len(_R33_LINES) + 1, line=2)], version="surrealdb-2.7.0")
        _offline(monkeypatch, client, dataclasses.replace(_read_t(), issues=(issue,)))
        assert await MigrationExecutor(tmp_path).migrate() == [name]
        assert _sent(client.sent[0])[1:] == [*_R33_LINES, "DEFINE FIELD `box` ON `t` TYPE any FLEXIBLE;"]

    @pytest.mark.parametrize(
        "operation",
        [AddField("t", "box", "any"), AlterField("t", "bag", "array<object>", previous_type="any"), AlterField("t", "bag")],
        ids=["add-any", "alter-objects", "alter-untyped"],
    )
    @pytest.mark.parametrize("issue", [_TABLE_ISSUE, _TABLES_ISSUE], ids=["table", "tables"])
    @pytest.mark.parametrize("version", _VERSIONS)
    async def test_a_flexible_candidate_on_an_unread_table(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, operation: Operation, issue: SchemaIssue, version: str
    ) -> None:
        name = _write(tmp_path, "add", [CreateTable("x", "SCHEMALESS"), operation])
        client = _FakeClient([_ok(3, line=_line_of(version))], version=version)
        _offline(monkeypatch, client, _read_t_with(issue))
        executor = MigrationExecutor(tmp_path)
        if _line_of(version) == 3:
            assert await executor.migrate() == [name]
            return
        with pytest.raises(SurrealDbError) as raised:
            await executor.migrate()
        assert str(raised.value) == _e_i1(name, 2, operation, "t")
        assert client.sent == []

    @pytest.mark.parametrize(
        ("operations", "issue"),
        [
            ([AddField("t", "label", "string")], _TABLE_ISSUE),
            ([AddField("t", "box", "any", flexible=True)], _TABLE_ISSUE),
            ([AddField("t", "box", "any")], _FIELD_ISSUE),
            (
                [AlterTable("t", "SCHEMAFULL", "normal", previous_schema_mode="SCHEMAFULL"), AddField("t", "box", "any")],
                _TABLE_ISSUE,
            ),
            (
                [
                    RawSQL("DEFINE TABLE OVERWRITE t SCHEMAFULL;", state_operations=[CreateTable("t")]),
                    AddField("t", "box", "any"),
                ],
                _TABLES_ISSUE,
            ),
        ],
        ids=["string", "flexible", "field-issue", "defined-before", "raw-sql-defines"],
    )
    async def test_a_field_whose_flexible_is_not_chosen_from_an_unread_table(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, operations: list[Operation], issue: SchemaIssue
    ) -> None:
        name = _write(tmp_path, "add", operations)
        client = _FakeClient([lambda fake: _ok(_sent_count(fake), line=2)], version="surrealdb-2.7.0")
        _offline(monkeypatch, client, _read_t_with(issue))
        assert await MigrationExecutor(tmp_path).migrate() == [name]

    async def test_a_table_an_earlier_migration_defines(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        first = _write(tmp_path, "a", [CreateTable("u")])
        second = _write(tmp_path, "b", [AddField("u", "bag", "any")], [first])
        client = _FakeClient([_ok(2, line=2), _ok(2, line=2)], version="surrealdb-2.7.0")
        _offline(monkeypatch, client, SchemaState(issues=[SchemaIssue("table", "u", "u", "INFO FOR TABLE failed")]))
        assert await MigrationExecutor(tmp_path).migrate() == [first, second]
        assert _sent(client.sent[1])[1:] == ["DEFINE FIELD `bag` ON `u` TYPE any FLEXIBLE;"]

    @pytest.mark.parametrize("issue", [_FIELD_ISSUE, _TABLES_ISSUE], ids=["field", "tables"])
    @pytest.mark.parametrize("version", _VERSIONS)
    async def test_a_rollback_restoring_schemafull(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, issue: SchemaIssue, version: str
    ) -> None:
        name = _write(tmp_path, "schemaless", [R33_REVERSE])
        line = _line_of(version)
        client = _recording([_ok(1 + (len(_R33_LINES) if line == 2 else 1), line=line)], name, version=version)
        _offline(monkeypatch, client, dataclasses.replace(_read_t(), issues=(issue,)))
        executor = MigrationExecutor(tmp_path)
        if line == 3:
            assert await executor.rollback("") == [name]
            return
        with pytest.raises(SurrealDbError) as raised:
            await executor.rollback("")
        assert str(raised.value) == _e_i1(name, 1, R33_REVERSE, "t")
        assert client.sent == []

    @pytest.mark.parametrize("version", _VERSIONS)
    async def test_a_rollback_redefining_a_flexible_candidate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, version: str
    ) -> None:
        alteration = AlterField("t", "bag", "string", previous_type="any")
        name = _write(tmp_path, "bag_text", [alteration])
        client = _recording([_ok(2, line=_line_of(version))], name, version=version)
        _offline(monkeypatch, client, _read_t_with(_TABLE_ISSUE))
        executor = MigrationExecutor(tmp_path)
        if _line_of(version) == 3:
            assert await executor.rollback("") == [name]
            return
        with pytest.raises(SurrealDbError) as raised:
            await executor.rollback("")
        assert str(raised.value) == _e_i1(name, 1, alteration, "t")
        assert client.sent == []

    async def test_a_rollback_whose_earlier_backwards_defines_the_table(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        first = _write(tmp_path, "bag_text", [AlterField("t", "bag", "string", previous_type="any")])
        # Undone first, the later migration's backwards redefines t (SCHEMALESS): the earlier one's FLEXIBLE is chosen
        # from that definition, not from the read.
        loose = AlterTable("t", "SCHEMALESS", "normal", previous_schema_mode="SCHEMALESS", previous_table_type="normal")
        second = _write(tmp_path, "schemaless", [loose], [first])
        client = _recording([_ok(2, line=2), _ok(2, line=2)], first, second, version="surrealdb-2.7.0")
        _offline(monkeypatch, client, _read_t_with(_TABLE_ISSUE))
        assert await MigrationExecutor(tmp_path).rollback("") == [second, first]


def _sent_count(client: _FakeClient) -> int:
    """The number of statements of the batch *client* was sent last (without ``BEGIN``/``COMMIT``)."""
    return len(_sent(client.sent[-1]))


def _with_issue(monkeypatch: pytest.MonkeyPatch, issue: SchemaIssue, **replaced: Any) -> None:
    """``DatabaseIntrospector.introspect()`` returns the real read plus *issue* — and the *replaced* sections."""
    real = DatabaseIntrospector.introspect

    async def read(self: DatabaseIntrospector) -> SchemaState:
        state = await real(self)
        return dataclasses.replace(state, issues=(*state.issues, issue), **replaced)

    monkeypatch.setattr(DatabaseIntrospector, "introspect", read)


async def _nothing_applied(
    client: Any, executor: MigrationExecutor, before: dict[str, Any], applied: Sequence[str] = ()
) -> None:
    """Nothing was sent: the tracking table records *applied*, ``INFO FOR DB`` is *before*."""
    assert await executor.get_applied_migrations() == list(applied)
    assert await _info_db(client) == before


async def _ready(client: Any, executor: MigrationExecutor) -> dict[str, Any]:
    """``INFO FOR DB`` once the tracking table exists."""
    await executor.ensure_migrations_table()
    return await _info_db(client)


class LdItem(BaseSurrealModel):
    """A SCHEMAFULL model with an ``Any`` column and an untyped list (``FLEXIBLE`` below 3, v0.34.0)."""

    model_config = SurrealConfigDict(table_name="ld_item", schema_mode=SchemaMode.SCHEMAFULL)

    id: str | None = None
    bag: Any = None
    tags: list = []  # type: ignore[type-arg]


class TestLineDispatchE2E:
    """AC-16 (spec §4): what the line chooses — the full-text keyword, ``FLEXIBLE`` on the ``any`` columns of a
    ``SCHEMAFULL`` table —, the 3.x-only clauses refused before any DDL below 3 (E-D1, E-D2, E-D3, a rollback's
    included) and applied on 3.x, and a primary-key user model's access, whose SIGNUP sets the record id."""

    @pytest.mark.parametrize("kind", _WS_KINDS)
    async def test_the_full_text_keyword(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind) as client:
            operations: list[Operation] = [
                DefineAnalyzer("ld_an", ["blank"], ["lowercase"]),
                CreateTable("ld_doc", "SCHEMAFULL"),
                AddField("ld_doc", "title", "string"),
                CreateIndex("ld_doc", "ld_title", ["title"], search_analyzer="ld_an", bm25=True),
            ]
            name = _write(tmp_path, "docs", operations)
            assert await MigrationExecutor(tmp_path).migrate() == [name]
            info = await client.query("INFO FOR TABLE ld_doc;", {})
            stored = info["indexes"]["ld_title"]
            keyword, other = ("FULLTEXT", "SEARCH") if await _line(client) >= 3 else ("SEARCH", "FULLTEXT")
            assert keyword in stored
            assert other not in stored

    @pytest.mark.parametrize("kind", _WS_KINDS)
    async def test_flexible_on_the_any_columns_of_a_schemafull_table(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind) as client:
            path = await makemigrations("items", migrations_dir=tmp_path, models=[LdItem], from_db=False)
            assert path is not None
            assert await MigrationExecutor(tmp_path).migrate() == [path.stem]
            fields = (await DatabaseIntrospector().introspect()).tables["ld_item"].fields
            below_3 = await _line(client) < 3
            assert (fields["bag"].flexible, fields["tags"].flexible) == (below_3, below_3)
            item = LdItem(id="one", bag={"a": {"b": 1}}, tags=[{"c": 1}])
            await item.save()
            await item.refresh()
            assert (item.bag, item.tags) == ({"a": {"b": 1}}, [{"c": 1}])

    @pytest.mark.parametrize(
        ("operation", "needs", "stored"),
        [
            (
                DefineEvent("ld_e", "ld_t", None, "CREATE ld_log SET a = 1", async_mode=True),
                _ASYNC_EVENTS,
                ("events", "ld_e", "ASYNC"),
            ),
            (
                DefineAccess(
                    "ld_acc",
                    "ld_t",
                    {"email": "$email", "password": "crypto::argon2::generate($password)"},
                    "email = $email AND crypto::argon2::compare(password, $password)",
                    with_refresh=True,
                ),
                _REFRESH_TOKENS,
                ("accesses", "ld_acc", "WITH REFRESH"),
            ),
            (AddField("ld_t", "c", "int", computed="a + 1"), _COMPUTED_FIELDS, ("fields", "c", "COMPUTED")),
        ],
        ids=["async", "refresh", "computed"],
    )
    @pytest.mark.parametrize("kind", _WS_KINDS)
    async def test_a_line_only_clause(
        self, kind: str, tmp_path: Path, operation: Operation, needs: str, stored: tuple[str, str, str]
    ) -> None:
        async with migration_database(kind) as client:
            operations: list[Operation] = [CreateTable("ld_t", "SCHEMALESS"), AddField("ld_t", "a", "int"), operation]
            name = _write(tmp_path, "line_only", operations)
            executor = MigrationExecutor(tmp_path)
            before = await _ready(client, executor)
            if await _line(client) >= 3:
                assert await executor.migrate() == [name]
                section, key, clause = stored
                info = await _info_db(client) if section == "accesses" else await client.query("INFO FOR TABLE ld_t;", {})
                assert clause in info[section][key]
                return
            with pytest.raises(SurrealDbError) as raised:
                await executor.migrate()
            assert type(raised.value) is SurrealDbError
            assert str(raised.value) == _e_d(name, 3, operation, needs)
            await _nothing_applied(client, executor, before)

    @pytest.mark.parametrize("kind", _WS_KINDS)
    async def test_a_rollback_restoring_a_computed_field(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind) as client:
            first = _write(tmp_path, "a", [CreateTable("ld_r", "SCHEMALESS"), AddField("ld_r", "a", "int")])
            alteration = AlterField("ld_r", "c", "int", previous_type="int", previous_computed="a + 1")
            second = _write(tmp_path, "b", [alteration], [first])
            executor = MigrationExecutor(tmp_path)
            assert await executor.migrate() == [first, second]
            before = await _info_db(client)
            fields = await _fields(client, "ld_r")
            if await _line(client) >= 3:
                assert await executor.rollback(first) == [second]
                assert "COMPUTED a + 1" in (await _fields(client, "ld_r"))["c"]
                return
            with pytest.raises(SurrealDbError) as raised:
                await executor.rollback(first)
            assert str(raised.value) == _e_d(second, 1, alteration, _COMPUTED_FIELDS)
            await _nothing_applied(client, executor, before, [first, second])
            assert await _fields(client, "ld_r") == fields

    @pytest.mark.parametrize("kind", _WS_KINDS)
    async def test_a_primary_key_user_models_access(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind) as client:
            path = await makemigrations("users", migrations_dir=tmp_path, models=[MigUser], from_db=False)
            assert path is not None
            assert await MigrationExecutor(tmp_path).migrate() == [path.stem]
            result = await MigUser.signup(email="pk@x.io", password="p", bind=kind == "mem")
            assert result.user.email == "pk@x.io"
            assert await client.query("SELECT VALUE id FROM mig_user;", {}) == [_sdk.RecordID("mig_user", "pk@x.io")]


#: The columns ``table_ddl()`` writes for ``Any``, ``list`` and ``list[Sub]`` (spec M27), on a SCHEMALESS table.
_AF_TABLE = CreateTable("af_t", "SCHEMALESS", "normal")
_AF_COLUMNS: list[Operation] = [
    AddField("af_t", "name", "string"),
    AddField("af_t", "bag", "any"),
    AddField("af_t", "tags", "array"),
    AddField("af_t", "tags.*", "any"),
    AddField("af_t", "subs", "array<object>"),
    AddField("af_t", "subs.*.k", "any"),
]
_AF_FLEXIBLE = ("bag", "tags", "tags.*", "subs", "subs.*.k")
_AF_SCHEMAFULL = AlterTable("af_t", "SCHEMAFULL", "normal", previous_schema_mode="SCHEMALESS", previous_table_type="normal")
_AF_SCHEMALESS = AlterTable("af_t", "SCHEMALESS", "normal", previous_schema_mode="SCHEMAFULL", previous_table_type="normal")
_AF_OBJECTS = "bag = { x: { y: 1 } }, tags = [{ a: 1 }], subs = [{ k: { z: 1 } }]"
_AF_VALUES = {"bag": {"x": {"y": 1}}, "tags": [{"a": 1}], "subs": [{"k": {"z": 1}}]}


async def _af_kept(client: Any, *records: str) -> None:
    """After one more write, each of *records* still holds the objects of ``_AF_OBJECTS``."""
    for record in records:
        await _run(client, f"UPDATE af_t:{record} SET name = 'again';")
        (row,) = await client.query(f"SELECT bag, tags, subs FROM af_t:{record};", {})
        assert {key: row[key] for key in _AF_VALUES} == _AF_VALUES, record


def _redefinitions(batch: str) -> list[str]:
    return [statement for statement in _sent(batch) if statement.startswith("DEFINE FIELD")]


class AfLoose(BaseSurrealModel):
    """A SCHEMALESS model with an ``Any`` column: ``define_table()`` writes ``bag TYPE any``, no migration file."""

    model_config = SurrealConfigDict(table_name="af_l", schema_mode=SchemaMode.SCHEMALESS)

    id: str | None = None
    name: str = ""
    bag: Any = None


class TestAlterTableFlexibleE2E:
    """M27, D15 (AC-16, AC-16 +'s hand-written part): an ``AlterTable`` making a table holding objects ``SCHEMAFULL``
    — or a backwards restoring it — redefines ``FLEXIBLE`` below 3 the columns the **database** holds that would lose
    them, so the objects written before and after are kept; on 3.x the table statement comes alone and the objects are
    kept too. ``show_sql()`` renders from the files: a column no file defines is not listed."""

    @pytest.mark.parametrize("kind", _WS_KINDS)
    async def test_a_table_a_migration_made(self, kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        async with migration_database(kind) as client:
            line = await _line(client)
            first = _write(tmp_path, "table", [_AF_TABLE, *_AF_COLUMNS])
            second = _write(tmp_path, "schemafull", [_AF_SCHEMAFULL], [first])
            executor = MigrationExecutor(tmp_path)
            assert await executor.migrate(target=first) == [first]
            await _run(client, f"UPSERT af_t:one SET name = 'a', {_AF_OBJECTS};")
            shown = await executor.show_sql(second, line=line)
            expected = [statement for statement in shown.split("\n") if statement.startswith("DEFINE FIELD")]
            assert len(expected) == (len(_AF_FLEXIBLE) if line == 2 else 0)
            sent = _send_spy(monkeypatch)
            for attempt in ("migrate", "again"):
                assert await executor.migrate() == [second], attempt
                (batch,) = [text for text in sent if _TRACKING in text]
                assert _sent(batch)[1] == _AF_SCHEMAFULL.forwards()
                redefined = _redefinitions(batch)
                assert [statement.split(" ON ")[0] for statement in redefined] == [
                    statement.split(" ON ")[0] for statement in expected
                ], attempt
                assert all(statement.endswith(" FLEXIBLE;") for statement in redefined)
                fields = (await DatabaseIntrospector().introspect()).tables["af_t"].fields
                assert all(fields[column].flexible is (line == 2) for column in _AF_FLEXIBLE), attempt
                await _run(client, f"UPSERT af_t:two SET name = 'b', {_AF_OBJECTS};")
                await _af_kept(client, "one", "two")
                if attempt == "migrate":
                    assert await executor.rollback(first) == [second]
                    sent.clear()

    @pytest.mark.parametrize("kind", _WS_KINDS)
    async def test_a_backwards_restoring_schemafull(self, kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        async with migration_database(kind) as client:
            line = await _line(client)
            first = _write(tmp_path, "table", [_AF_TABLE, *_AF_COLUMNS])
            second = _write(tmp_path, "schemafull", [_AF_SCHEMAFULL], [first])
            third = _write(tmp_path, "schemaless", [_AF_SCHEMALESS], [second])
            executor = MigrationExecutor(tmp_path)
            assert await executor.migrate() == [first, second, third]
            await _run(client, f"UPSERT af_t:one SET name = 'a', {_AF_OBJECTS};")
            sent = _send_spy(monkeypatch)
            assert await executor.rollback(second) == [third]
            (batch,) = [text for text in sent if _UNRECORD in text]
            assert _sent(batch)[1] == _AF_SCHEMALESS.backwards()
            assert len(_redefinitions(batch)) == (len(_AF_FLEXIBLE) if line == 2 else 0)
            shown = await executor.show_sql(third, backwards=True, line=line)
            assert len([statement for statement in shown.split("\n") if statement.startswith("DEFINE FIELD")]) == len(
                _redefinitions(batch)
            )
            assert "SCHEMAFULL" in (await _info_db(client))["tables"]["af_t"]
            await _run(client, f"UPSERT af_t:two SET name = 'b', {_AF_OBJECTS};")
            await _af_kept(client, "one", "two")

    @pytest.mark.parametrize("kind", _WS_KINDS)
    async def test_a_table_define_table_made(self, kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        async with migration_database(kind) as client:
            line = await _line(client)
            await AfLoose.define_table()
            await AfLoose(id="one", bag={"x": {"y": 1}}).save()
            schemafull = AlterTable("af_l", "SCHEMAFULL", "normal", previous_schema_mode="SCHEMALESS")
            name = _write(tmp_path, "schemafull", [schemafull])
            executor = MigrationExecutor(tmp_path)
            # The files hold no column of af_l: show_sql() gives the table statement alone on either line.
            assert await executor.show_sql(name, line=2) == schemafull.forwards(line=2)
            sent = _send_spy(monkeypatch)
            assert await executor.migrate() == [name]
            (batch,) = [text for text in sent if _TRACKING in text]
            expected = ["DEFINE FIELD OVERWRITE `bag` ON `af_l` TYPE any FLEXIBLE;"] if line == 2 else []
            assert _redefinitions(batch) == expected
            assert ("FLEXIBLE" in (await _fields(client, "af_l"))["bag"]) is (line == 2)
            await AfLoose(id="two", bag={"x": {"y": 2}}).save()
            await _run(client, "UPDATE af_l:one SET name = 'again';")
            assert await client.query("SELECT VALUE bag FROM af_l:one;", {}) == [{"x": {"y": 1}}]
            assert await client.query("SELECT VALUE bag FROM af_l:two;", {}) == [{"x": {"y": 2}}]


_GD_ANALYZER = Analyzer("gd_x", ["blank"], ["lowercase"])


class GdDoc(BaseSurrealModel):
    """A full-text index and its analyzer made by ``define_indexes()``, no migration defining them."""

    model_config = SurrealConfigDict(
        table_name="gd_doc",
        schema_mode=SchemaMode.SCHEMAFULL,
        indexes=[Index("gd_title", "title", search_analyzer=_GD_ANALYZER, bm25=True)],
    )

    id: str | None = None
    title: str = ""


class GdView(BaseSurrealModel):
    """A view made by ``define_view()``, no migration defining it, reading ``gd_src``."""

    model_config = SurrealConfigDict(table_name="gd_view", view_query="SELECT count() AS n FROM gd_src GROUP ALL")

    id: str | None = None
    n: int = 0


class TestGuardsE2E:
    """AC-17, AC-17 + (D13, C8): E-G1 (an analyzer an index still uses) and E-G2 (a table a view reads) before any DDL
    on every engine — the index or the view made by an earlier migration, by ``define_indexes()``/``define_view()``
    outside migrations, or met by a rollback's backwards; nothing recorded, nothing removed."""

    @pytest.mark.parametrize("kind", _WS_KINDS)
    async def test_an_analyzer_an_earlier_migration_put_in_use(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind) as client:
            first = _write(
                tmp_path,
                "docs",
                [
                    DefineAnalyzer("gd_an", ["blank"], ["lowercase"]),
                    CreateTable("gd_m", "SCHEMAFULL"),
                    AddField("gd_m", "title", "string"),
                    CreateIndex("gd_m", "gd_m_title", ["title"], search_analyzer="gd_an", bm25=True),
                ],
            )
            removal = RemoveAnalyzer("gd_an")
            second = _write(tmp_path, "drop", [removal, DropIndex("gd_m", "gd_m_title")], [first])
            executor = MigrationExecutor(tmp_path)
            assert await executor.migrate(target=first) == [first]
            before = await _info_db(client)
            with pytest.raises(ValueError) as raised:
                await executor.migrate()
            assert str(raised.value) == _e_g1(second, 1, removal, "gd_m_title", "gd_m")
            await _nothing_applied(client, executor, before, [first])

    @pytest.mark.parametrize("kind", _WS_KINDS)
    async def test_an_analyzer_define_indexes_put_in_use(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind) as client:
            await GdDoc.define_table()
            await GdDoc.define_indexes()
            removal = RemoveAnalyzer("gd_x")
            name = _write(tmp_path, "drop", [removal])
            executor = MigrationExecutor(tmp_path)
            before = await _ready(client, executor)
            with pytest.raises(ValueError) as raised:
                await executor.migrate()
            assert str(raised.value) == _e_g1(name, 1, removal, "gd_title", "gd_doc")
            await _nothing_applied(client, executor, before)

    @pytest.mark.parametrize("kind", _WS_KINDS)
    async def test_a_rollback_removing_an_analyzer_in_use(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind) as client:
            definition = DefineAnalyzer("gd_x", ["blank"], ["lowercase"])
            name = _write(tmp_path, "analyzer", [definition])
            executor = MigrationExecutor(tmp_path)
            assert await executor.migrate() == [name]
            await GdDoc.define_table()
            await GdDoc.define_indexes()
            before = await _info_db(client)
            with pytest.raises(ValueError) as raised:
                await executor.rollback("")
            assert str(raised.value) == _e_g1(name, 1, definition, "gd_title", "gd_doc")
            await _nothing_applied(client, executor, before, [name])

    @pytest.mark.parametrize("kind", _WS_KINDS)
    async def test_a_view_an_earlier_migration_made(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind) as client:
            view = CreateTable("gd_mv", None, view_query="SELECT count() AS n FROM gd_src GROUP ALL")
            first = _write(tmp_path, "tables", [CreateTable("gd_src", "SCHEMALESS"), view])
            drop = DropTable("gd_src")
            second = _write(tmp_path, "drop", [drop], [first])
            executor = MigrationExecutor(tmp_path)
            assert await executor.migrate(target=first) == [first]
            before = await _info_db(client)
            with pytest.raises(ValueError) as raised:
                await executor.migrate()
            assert str(raised.value) == _e_g2(second, 1, drop, "gd_mv")
            await _nothing_applied(client, executor, before, [first])

    @pytest.mark.parametrize("kind", _WS_KINDS)
    async def test_a_view_define_view_made(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind) as client:
            await _run(client, "DEFINE TABLE gd_src SCHEMALESS;")
            await GdView.define_view()
            drop = DropTable("gd_src")
            name = _write(tmp_path, "drop", [drop])
            executor = MigrationExecutor(tmp_path)
            before = await _ready(client, executor)
            with pytest.raises(ValueError) as raised:
                await executor.migrate()
            assert str(raised.value) == _e_g2(name, 1, drop, "gd_view")
            await _nothing_applied(client, executor, before)

    @pytest.mark.parametrize("kind", _WS_KINDS)
    async def test_a_rollback_removing_a_views_source(self, kind: str, tmp_path: Path) -> None:
        async with migration_database(kind) as client:
            creation = CreateTable("gd_src", "SCHEMALESS")
            name = _write(tmp_path, "source", [creation])
            executor = MigrationExecutor(tmp_path)
            assert await executor.migrate() == [name]
            await GdView.define_view()
            before = await _info_db(client)
            with pytest.raises(ValueError) as raised:
                await executor.rollback("")
            assert str(raised.value) == _e_g2(name, 1, creation, "gd_view")
            await _nothing_applied(client, executor, before, [name])


#: A table made by hand for ``TestReadIssuesE2E``: ``SCHEMAFULL``, with a ``label`` and an ``any`` column ``bag``.
_RI_TABLE = "DEFINE TABLE t TYPE NORMAL SCHEMAFULL; DEFINE FIELD label ON t TYPE string; DEFINE FIELD bag ON t TYPE any;"


def _issue(kind: str, name: str, table: str | None) -> SchemaIssue:
    return SchemaIssue(kind, name, table, "could not parse its definition")


class TestReadIssuesE2E:
    """C8, D38, P1–P3 (AC-16 +, AC-9 +): the real read plus one ``SchemaIssue``. E-I1 below 3 — never on 3.x — when a
    ``FLEXIBLE`` choice would be made from a table the read could not read entirely; E-I2 on every engine when the
    database's definition of an analyzer phase A redefines cannot be read (an ``analyzer`` issue, the ``analyzers``
    section issue) or written by the ORM. Each refusal comes before any DDL: no record, ``INFO FOR DB`` unchanged."""

    @pytest.mark.parametrize(
        "issue", [_issue("field", "lost", "t"), _issue("section", "tables", None)], ids=["field", "tables"]
    )
    @pytest.mark.parametrize("kind", _WS_KINDS)
    async def test_a_table_made_schemafull(
        self, kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, issue: SchemaIssue
    ) -> None:
        async with migration_database(kind) as client:
            await _run(client, _RI_TABLE)
            name = _write(tmp_path, "schemafull", [R33])
            executor = MigrationExecutor(tmp_path)
            before = await _ready(client, executor)
            _with_issue(monkeypatch, issue)
            if await _line(client) >= 3:
                assert await executor.migrate() == [name]
                return
            with pytest.raises(SurrealDbError) as raised:
                await executor.migrate()
            assert str(raised.value) == _e_i1(name, 1, R33, "t")
            await _nothing_applied(client, executor, before)

    @pytest.mark.parametrize("kind", _WS_KINDS)
    async def test_an_any_column_added_to_an_unread_table(
        self, kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async with migration_database(kind) as client:
            await _run(client, _RI_TABLE)
            addition = AddField("t", "box", "any")
            name = _write(tmp_path, "box", [addition])
            executor = MigrationExecutor(tmp_path)
            before = await _ready(client, executor)
            _with_issue(monkeypatch, _issue("table", "t", "t"))
            if await _line(client) >= 3:
                assert await executor.migrate() == [name]
                return
            with pytest.raises(SurrealDbError) as raised:
                await executor.migrate()
            assert str(raised.value) == _e_i1(name, 1, addition, "t")
            await _nothing_applied(client, executor, before)

    @pytest.mark.parametrize(
        ("addition", "issue", "defined_before"),
        [
            (AddField("t", "name", "string"), _issue("table", "t", "t"), False),
            (AddField("t", "box", "any"), _issue("field", "label", "t"), False),
            (AddField("t", "box", "any"), _issue("table", "other", "other"), False),
            (AddField("u", "box", "any"), _issue("table", "u", "u"), True),
        ],
        ids=["string", "field-issue", "other-table", "defined-by-the-plan"],
    )
    @pytest.mark.parametrize("kind", _WS_KINDS)
    async def test_a_field_applied_on_every_line(
        self,
        kind: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        addition: AddField,
        issue: SchemaIssue,
        defined_before: bool,
    ) -> None:
        async with migration_database(kind) as client:
            await _run(client, _RI_TABLE)
            names: list[str] = []
            if defined_before:  # a migration before the field's defines u, which the database does not hold
                names.append(_write(tmp_path, "table", [CreateTable("u")]))
            names.append(_write(tmp_path, "field", [addition], names))
            _with_issue(monkeypatch, issue)
            assert await MigrationExecutor(tmp_path).migrate() == names
            column = (await _fields(client, addition.table))[addition.name]
            if addition.field_type == "any":  # FLEXIBLE below 3, chosen from the read or from the plan
                assert ("FLEXIBLE" in column) is (await _line(client) < 3)

    @pytest.mark.parametrize("kind", _WS_KINDS)
    async def test_a_rollback_redefining_an_any_column_of_an_unread_table(
        self, kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async with migration_database(kind) as client:
            await _run(client, _RI_TABLE)
            alteration = AlterField("t", "bag", "string", previous_type="any")
            name = _write(tmp_path, "bag_text", [alteration])
            executor = MigrationExecutor(tmp_path)
            assert await executor.migrate() == [name]
            before = await _info_db(client)
            _with_issue(monkeypatch, _issue("table", "t", "t"))
            if await _line(client) >= 3:
                assert await executor.rollback("") == [name]
                assert "TYPE any" in (await _fields(client, "t"))["bag"]
                return
            with pytest.raises(SurrealDbError) as raised:
                await executor.rollback("")
            assert str(raised.value) == _e_i1(name, 1, alteration, "t")
            await _nothing_applied(client, executor, before, [name])

    @pytest.mark.parametrize(
        "issue", [_issue("analyzer", "ri_an", None), _issue("section", "analyzers", None)], ids=["analyzer", "section"]
    )
    @pytest.mark.parametrize("kind", _WS_KINDS)
    async def test_an_unreadable_analyzer_phase_a_redefines(
        self, kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, issue: SchemaIssue
    ) -> None:
        async with migration_database(kind) as client:
            await _run(client, "DEFINE ANALYZER ri_an TOKENIZERS blank FILTERS ascii;")
            name = _write(tmp_path, "analyzer", [DefineAnalyzer("ri_an", ["blank"], ["lowercase"])])
            executor = MigrationExecutor(tmp_path)
            before = await _ready(client, executor)
            _with_issue(monkeypatch, issue)
            with pytest.raises(SurrealDbError) as raised:
                await executor.migrate()
            assert str(raised.value) == _ri_e_i2(name, "Define analyzer ri_an")
            await _nothing_applied(client, executor, before)

    @pytest.mark.parametrize("kind", _WS_KINDS)
    async def test_an_unreadable_analyzer_a_rollback_restores(
        self, kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async with migration_database(kind) as client:
            definition = DefineAnalyzer("ri_an", ["blank"], ["lowercase"])
            first = _write(tmp_path, "a", [definition])
            second = _write(tmp_path, "b", [DefineAnalyzer("ri_an", ["blank"], ["ascii"], previous=definition)], [first])
            executor = MigrationExecutor(tmp_path)
            assert await executor.migrate() == [first, second]
            before = await _info_db(client)
            _with_issue(monkeypatch, _issue("section", "analyzers", None))
            with pytest.raises(SurrealDbError) as raised:
                await executor.rollback(first)
            assert str(raised.value) == _ri_e_i2(second, "Define analyzer ri_an")
            await _nothing_applied(client, executor, before, [first, second])

    @pytest.mark.parametrize("kind", _WS_KINDS)
    async def test_an_analyzer_the_orm_cannot_write(self, kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        async with migration_database(kind) as client:
            await _run(client, "DEFINE ANALYZER ri_an TOKENIZERS blank FILTERS ascii;")
            name = _write(tmp_path, "analyzer", [DefineAnalyzer("ri_an", ["blank"], ["lowercase"])])
            executor = MigrationExecutor(tmp_path)
            before = await _ready(client, executor)
            odd = AnalyzerState("ri_an", ("blank",), ("edgengram(10,2)",))
            real = DatabaseIntrospector.introspect

            async def read(self: DatabaseIntrospector) -> SchemaState:
                state = await real(self)
                return dataclasses.replace(state, analyzers={**state.analyzers, "ri_an": odd})

            monkeypatch.setattr(DatabaseIntrospector, "introspect", read)
            with pytest.raises(SurrealDbError) as raised:
                await executor.migrate()
            assert str(raised.value) == _ri_e_i2(name, "Define analyzer ri_an")
            await _nothing_applied(client, executor, before)


def _ri_e_i2(name: str, describe: str, analyzer: str = "ri_an") -> str:
    return (
        f"Migration '{name}', operation 1 ({describe}): the database's definition of analyzer '{analyzer}' cannot be "
        "read or written by the ORM, so it could not be restored if the migration failed; nothing was applied. "
        "Redefine the analyzer in a form search.Analyzer accepts (by hand, or with define_indexes()), then run the call "
        "again."
    )


class TestUnwritableAnalyzerE2E:
    """P3, M32 (AC-9 +): an analyzer defined by hand with ``edgengram(10,2)`` — stored by SurrealDB 3.3.4 and 2.7.0,
    refused by ``search.Analyzer`` — cannot be restored by phase A's compensation, so a migration redefining it is
    refused before any DDL (E-I2, its remedy included); redefined by hand in a form ``search.Analyzer`` accepts, the
    same ``migrate()`` applies."""

    async def test_refused_then_applied_once_redefined(self, tmp_path: Path) -> None:
        async with migration_database("ws") as client:
            await _run(client, "DEFINE ANALYZER odd TOKENIZERS blank FILTERS edgengram(10,2);")
            name = _write(tmp_path, "odd", [DefineAnalyzer("odd", tokenizers=["blank"], filters=["lowercase"])])
            executor = MigrationExecutor(tmp_path)
            before = await _ready(client, executor)
            with pytest.raises(SurrealDbError) as raised:
                await executor.migrate()
            assert str(raised.value) == _ri_e_i2(name, "Define analyzer odd", "odd")
            await _nothing_applied(client, executor, before)
            await _run(client, "DEFINE ANALYZER OVERWRITE odd TOKENIZERS blank FILTERS edgengram(2,10);")
            assert await executor.migrate() == [name]
            read = await DatabaseIntrospector().introspect()
            assert read.analyzers["odd"].filters == ("lowercase",)
            assert await executor.get_applied_migrations() == [name]


#: Spec §9.2: whether the server accepts a ``REFERENCE`` field, per port (the embedded engine: ``"mem"``).
_REFERENCE_SUPPORT: dict[str, bool] = {}


async def reference_supported(kind: str) -> bool:
    """The capability probe for ``REFERENCE`` (spec §9.2, CLAUDE.md rule 1): a ``REFERENCE`` field defined in a
    throwaway database (``mig_probe_<uuid8>``, removed after) of the engine *kind* — never the version, since a 2.x
    server may enable the experimental ``record_references`` capability. Cached per port."""
    key = "mem" if kind == "mem" else os.environ.get("SURREALDB_PORT", "8000")
    if key not in _REFERENCE_SUPPORT:
        async with migration_database(kind, f"mig_probe_{uuid.uuid4().hex[:8]}") as client:
            statements = (
                "DEFINE TABLE `probe_owner`; DEFINE FIELD `o` ON `probe_pet` TYPE option<record<probe_owner>> REFERENCE;"
            )
            try:
                answer = await client.query_raw(statements, {})
            except RuntimeError:  # the embedded engine raises its parse errors
                supported = False
            else:
                supported = "error" not in answer and all(entry["status"] == "OK" for entry in answer["result"])
        _REFERENCE_SUPPORT[key] = supported
    return _REFERENCE_SUPPORT[key]


class TestReferenceE2E:
    """AC-16, spec §4: ``AddField(reference=True)`` is applied where the capability probe's ``REFERENCE`` field is
    accepted, and refused by the server elsewhere — E-X1 naming the operation through the parse error's position, its
    reason mentioning ``record_references``."""

    @pytest.mark.parametrize("kind", _WS_KINDS)
    async def test_a_reference_field(self, kind: str, tmp_path: Path) -> None:
        supported = await reference_supported(kind)
        async with migration_database(kind) as client:
            operations: list[Operation] = [
                CreateTable("rf_owner", "SCHEMALESS"),
                AddField("rf_pet", "o", "option<record<rf_owner>>", reference=True),
            ]
            name = _write(tmp_path, "reference", operations)
            executor = MigrationExecutor(tmp_path)
            if supported:
                assert await executor.migrate() == [name]
                assert "REFERENCE" in (await _fields(client, "rf_pet"))["o"]
                return
            with pytest.raises(MigrationStatementError) as raised:
                await executor.migrate()
            assert (raised.value.operation, raised.value.description) == (2, "Add field o to rf_pet")
            assert str(raised.value).startswith(f"Migration '{name}' failed at operation 2 of 2 (Add field o to rf_pet): ")
            assert "record_references" in str(raised.value)
            assert await executor.get_applied_migrations() == []
