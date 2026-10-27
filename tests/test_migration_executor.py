"""Migration system (v0.38.0): ``MigrationExecutor.migrate()`` — applying migrations atomically.

``migrate()`` (spec §5.6) loads and orders the migration files, ensures the tracking table ``_surreal_orm_migrations``,
reads the applied names and checks the history, then reads the database's schema **once** (``DatabaseIntrospector``),
replays the planned migrations over it strictly (E-R1 before any DDL) and renders every statement from those states
before the first one is sent. Each migration is applied in one ``BEGIN … COMMIT`` batch holding its tracking record
first (phase B); its analyzer definitions are committed in a batch of their own just before (phase A) and restored to
the definitions the database held when phase B fails (D38). A failed batch is read through the line's status layout
(``TestBatchStatus``: envelopes recorded on 3.3.4, 2.7.0 and the embedded engine), and the tracking table is read again
before anything else is decided: a concurrent run may have applied the migration, or the answer may have been lost.

Unit: ``TestBatchStatus``, ``TestLine``, ``TestCompensationUnit`` (a fake client replaying envelopes, a hand-built read).
E2E — the line under test over WebSocket and HTTP, and the embedded engine, each test in a database of its own
(``migration_database()``, spec §9.2): ``TestMigrateE2E`` (AC-8, AC-13), ``TestSchemaAnchorE2E`` (AC-8 bis's
hand-written part, AC-29's ``migrate()`` part), ``TestAtomicityE2E`` (AC-9), ``TestAnalyzerPhaseE2E`` (AC-10),
``TestCompensationE2E`` (AC-9, AC-9 +), ``TestConcurrencyE2E`` (AC-14), ``TestCacheE2E`` (AC-21),
``TestNoStatementLeakE2E`` (AC-22). Each E2E test chooses its expectation per line from ``cached_major()``; none skips.
"""

import asyncio
import contextlib
import logging
import os
import sys
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
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
    DefineAnalyzer,
    DropField,
    DropTable,
    Migration,
    MigrationExecutor,
    MigrationGenerator,
    MigrationStatementError,
    Operation,
    RawSQL,
    RebuildIndex,
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
from surreal_orm_lite.testing import embedded_database
from surreal_orm_lite.time_travel import cached_major
from tests.test_migration_diff import _MODEL_SET, MiBare, MiCommented, MiServer
from tests.test_migration_operations import (
    _R33_LINES,
    R33,
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


# === E2E: the line under test over WebSocket and HTTP, and the embedded engine ========================================

#: The engines every E2E class runs on.
_KINDS = ["ws", "http", "mem"]


def _url(kind: str) -> str:
    host = os.environ.get("SURREALDB_HOST", "localhost")
    port = os.environ.get("SURREALDB_PORT", "8000")
    return f"ws://{host}:{port}/rpc" if kind == "ws" else f"http://{host}:{port}"


def _database_name() -> str:
    return f"mig_{uuid.uuid4().hex[:8]}"


@contextlib.asynccontextmanager
async def migration_database(kind: str, database: str | None = None) -> AsyncIterator[Any]:
    """The default connection on a database of its own (spec §9.2): ``ns``/*database* (``mig_<uuid8>`` when ``None``)
    over WebSocket or HTTP on the line under test, removed on exit; or a fresh embedded engine (``mem://``). Yields the
    SDK client."""
    database = database or _database_name()
    if kind == "mem":
        async with embedded_database(namespace="ns", database=database) as client:
            yield client
        return
    SurrealDBConnectionManager.set_connection(url=_url(kind), user="root", password="root", namespace="ns", database=database)
    client = await SurrealDBConnectionManager.get_client()
    await client.query(f"REMOVE DATABASE IF EXISTS {database}; DEFINE DATABASE {database};", {})
    try:
        yield client
    finally:
        with contextlib.suppress(Exception):
            cleanup = await SurrealDBConnectionManager.get_client()
            await cleanup.query(f"REMOVE DATABASE IF EXISTS {database};", {})
        await SurrealDBConnectionManager.close_connection()


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
