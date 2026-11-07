"""Unit: the README-structure helpers of ``tests/_readme_tables.py``, on made-up headers.

The documentation tests rely on these helpers to fail when a release's text changes by one character, including the
punctuation that ends it and the word that starts it (issue #272, PR review).
"""

from __future__ import annotations

import pytest

from tests._readme_tables import (
    SERVER_4X_COLUMN,
    SERVER_BEHAVIOUR,
    SERVER_HEADER,
    assert_header_sentence,
    assert_server_header_sentence,
    assert_server_rows,
    assert_server_table,
    cells,
    doc_part,
    server_rows,
    server_rows_4x,
    tables,
)

_V1 = "rows added in v0.1.0 on 2.7.0 and 3.3.4, identical on 3.1.5"
_V2 = "the rows added in v0.2.0 on 2.7.0 and 3.3.4, with 3.1.5 where it differs"
_V3 = "rows added in v0.3.0 on 2.7.0 and 3.3.4 in one process, identical on 3.1.5"
_V4 = "The row added in v0.4.0 was measured on 2.7.0 and 3.3.4, identical on 3.1.5."
_V5 = "Rows added in v0.5.0 were measured on 2.7.0, 3.3.4 and the embedded engine, identical on 3.1.5."
_HEADER = f"### Table Intro. Rows added in v0.0.9 were measured on 2.7.0; {_V1}; {_V2}; {_V3}. {_V4} {_V5}"


class TestAssertHeaderSentence:
    @pytest.mark.parametrize("sentence", [_V1, _V2, _V3, _V4, _V5])
    def test_a_bounded_sentence_or_clause_passes(self, sentence: str) -> None:
        assert_header_sentence(_HEADER, sentence)

    def test_the_last_sentence_may_end_the_header(self) -> None:
        assert_header_sentence(_HEADER.removesuffix(f" {_V5}"), _V4)
        assert_header_sentence(_HEADER.removesuffix(f" {_V4} {_V5}"), _V3)

    @pytest.mark.parametrize(
        ("old", "new", "sentence"),
        [
            # A wrong measured version right after a clause or a sentence.
            pytest.param(f"{_V1};", f"{_V1}0;", _V1, id="3.1.50-after-a-clause"),
            pytest.param(_V5, f"{_V5[:-1]}0.", _V5, id="3.1.50-in-a-sentence"),
            # A clause no longer separated by "; " or ". ": its end, then the start of the next one.
            pytest.param(f"{_V1}; {_V2}", f"{_V1}, {_V2}", _V1, id="comma-ends-a-clause"),
            pytest.param(f"{_V1}; {_V2}", f"{_V1}, {_V2}", _V2, id="comma-starts-a-clause"),
            pytest.param(f"{_V3}. {_V4}", f"{_V3}, {_V4}", _V3, id="comma-ends-the-last-clause"),
            pytest.param(f"{_V3}. {_V4}", f"{_V3}, {_V4}", _V4, id="comma-starts-a-sentence"),
            # A word glued to the start of a clause or a sentence.
            pytest.param(f"; {_V2}", f"; a{_V2}", _V2, id="arrows"),
            pytest.param(f" {_V5}", f" A{_V5}", _V5, id="ARows"),
            # A sentence's full stop followed by anything but a space or the end of the header.
            pytest.param(f"{_V4} {_V5}", f"{_V4}x{_V5}", _V4, id="no-space-after-a-sentence"),
            # A clause going on past where it ends.
            pytest.param(f"{_V3}. {_V4}", f"{_V3} and more. {_V4}", _V3, id="a-clause-goes-on"),
        ],
    )
    def test_a_change_at_a_boundary_fails(self, old: str, new: str, sentence: str) -> None:
        assert _HEADER.count(old) == 1
        with pytest.raises(AssertionError):
            assert_header_sentence(_HEADER.replace(old, new), sentence)

    def test_a_changed_character_inside_fails(self) -> None:
        with pytest.raises(AssertionError):
            assert_header_sentence(_HEADER.replace("in one process", "in one proces"), _V3)

    def test_a_second_copy_fails(self) -> None:
        with pytest.raises(AssertionError):
            assert_header_sentence(f"{_HEADER} {_V5}", _V5)

    def test_releases_out_of_order_fail(self) -> None:
        swapped = _HEADER.replace(f"{_V1}; {_V2}", f"{_V2}; {_V1}")
        with pytest.raises(AssertionError, match="out of version order"):
            assert_header_sentence(swapped, _V1)

    @pytest.mark.parametrize("sentence", ["identical on 3.1.5", f"{_V1} and {_V2}"])
    def test_a_sentence_names_one_release(self, sentence: str) -> None:
        with pytest.raises(AssertionError, match="names one release"):
            assert_header_sentence(_HEADER, sentence)


# --- v0.50.0: the 4.x column of the 2.x/3.x/4.x table and its rows (spec §11, AC-15, AC-21, AC-22; D25, P5) ---------

#: v0.50.0's rows, all five cells, in order: the multi-statement row of Task 8, then the rows of spec §11.
_V050_ROWS = [
    [
        "`raw_query()` / `QuerySet.query()` with several statements",
        "same on both lines: the rows of every statement, in order (a `LET` or `RETURN` adds none); a failing later "
        "statement raises",
        "same as 2.x (3.x and 4.x)",
        "same as 2.x",
        "v0.50.0",
    ],
    [
        "`transaction()` strategy with the SDK 3.0",
        "buffered: the SDK refuses `begin()` up front, so no probe error",
        "interactive on WebSocket (the SDK's `Features.TRANSACTIONS`), buffered over HTTP",
        "as 3.x",
        "v0.50.0",
    ],
    [
        "A `NULL` read (model fields, `raw_query()`, `values()`, live and change-feed events)",
        "`None` on every line (never the SDK's `Null`)",
        "same as 2.x",
        "same as 2.x",
        "v0.50.0",
    ],
    [
        "A native set read (a `set<…>` column, a raw result)",
        "no set type: an array, which a set field reads back as its set",
        "3.3: a Python `set`; 3.1 refuses an array written to a `set<…>` column",
        "as 3.3",
        "v0.50.0",
    ],
    [
        "A datetime's nanoseconds, read and written back",
        "kept: read as a `PreciseDatetime` (a `datetime`), written back with its nanoseconds",
        "same as 2.x",
        "same as 2.x",
        "v0.50.0",
    ],
    ["A zero duration (`Duration(0)`)", "stored and read back as `Duration(0)`", "same as 2.x", "same as 2.x", "v0.50.0"],
    [
        "A `date` field",
        "stored as a datetime at midnight UTC, read back as the `date`",
        "same as 2.x",
        "same as 2.x: the ORM never sends 4.x's `date` kind",
        "v0.50.0",
    ],
    [
        "Server version outside the SDK's range (2.0.0 to 5.0.0 excluded)",
        "refused at connect time: `SurrealDbConnectionError` naming the version, not retryable",
        "same as 2.x",
        "same as 2.x",
        "v0.50.0",
    ],
    [
        "`all()`, `values()`, `limit()`/`offset()` without `order_by()`",
        "unordered by contract: id order for a plain table scan, index order for an indexed filter",
        "the same",
        "unordered (SurrealDB 4.0): add `order_by()` to page deterministically",
        "v0.50.0",
    ],
    [
        "`first()` without `order_by()`",
        "server order (id order for a plain table scan), unchanged",
        "`ORDER BY id` (smallest matching id)",
        "`ORDER BY id` (smallest matching id)",
        "v0.50.0",
    ],
    [
        "A WebSocket client the ORM retired (dropped, closed, re-registered), kept by the application",
        "its next request raises `SurrealDbConnectionError` and opens no socket; retryable after a drop only",
        "same as 2.x",
        "same as 2.x",
        "v0.50.0",
    ],
    [
        "An HTTP answer with a status other than 2xx, 408, 429, 502, 503 and 504 (a revoked token's 401)",
        "same on both lines: the server's answer, not a lost connection; `reconnect()` raises `SurrealDbAuthenticationError`",
        "same on both lines",
        "same as 3.x",
        "v0.50.0",
    ],
    [
        "Migration field change on a populated `SCHEMAFULL` table (a narrowed type, a new field refusing `NONE`)",
        "applied",
        "applied",
        "(compatibility level 4): narrowing a type or adding a field that refuses NONE raises "
        "FieldChangeRefusedError, refused inside the migration's transaction, before the field change runs; nothing "
        "is applied; empty tables and compatibility level 3 apply",
        "v0.50.0",
    ],
    [
        "Schema sync (`define_table(overwrite=True)`) on a populated `SCHEMAFULL` table",
        "applied",
        "applied",
        "the server's refusal, normalised; use a migration with `RawSQL`",
        "v0.50.0",
    ],
]
_HEADER_SENTENCE_50 = "Rows added in v0.50.0 were measured on 2.7.0, 3.3.5, 4.0.0 and the embedded engine."


class TestServerTable4xColumn:
    """The 2.x/3.x/4.x table of ``docs/compatibility.md`` has a 4.x column on every row (v0.50.0)."""

    def test_header(self) -> None:
        [lines] = tables(doc_part(SERVER_BEHAVIOUR))
        assert cells(lines[0]) == SERVER_HEADER

    def test_every_row_has_a_4x_cell(self) -> None:
        rows = server_rows_4x()
        assert len(rows) > 175 and all(len(row) == 5 and row[SERVER_4X_COLUMN] for row in rows), rows[:3]

    def test_the_release_view_leaves_the_4x_cell_out(self) -> None:
        assert server_rows() == [row[:3] + row[4:] for row in server_rows_4x()]
        assert_server_table()

    def test_the_v0_50_0_rows(self) -> None:
        """Cell by cell, together and in version order before the table's last row; the table keeps its widths."""
        assert_server_rows([row[:3] + row[4:] for row in _V050_ROWS])
        rows = server_rows_4x()
        start = rows.index(_V050_ROWS[0])
        assert rows[start : start + len(_V050_ROWS)] == _V050_ROWS

    def test_the_v0_50_0_header_sentence(self) -> None:
        assert_server_header_sentence(_HEADER_SENTENCE_50)

    def test_the_4x_column_says_how_it_was_measured(self) -> None:
        header = " ".join(doc_part(SERVER_BEHAVIOUR).split("\n|", 1)[0].split())
        assert "The 4.x column, new in v0.50.0, was measured by the suite on 4.0.0" in header, header
