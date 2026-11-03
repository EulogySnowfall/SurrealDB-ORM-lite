"""The Markdown documentation never ships with merge-conflict markers (issue #398).

A conflict resolved by hand in ``CHANGELOG.md`` once reached ``main`` with its ``<<<<<<<``, ``=======`` and
``>>>>>>>`` lines still in place: nothing in the suite reads the changelog, so the CI stayed green. This test scans
every tracked Markdown file (``CHANGELOG.md``, ``README.md``, ``SECURITY.md``, ``docs/**/*.md``…) and fails with the
``file:line`` of each marker, so a leftover conflict blocks the merge like any other red test.

The helper of ``tests/_markdown_markers.py`` is checked on temporary files, with and without markers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._markdown_markers import REPO_ROOT, conflict_markers, markdown_files

_REQUIRED = ["CHANGELOG.md", "README.md", "SECURITY.md", "docs/ROADMAP.md"]


class TestRepositoryMarkdown:
    def test_the_documentation_files_are_scanned(self) -> None:
        scanned = {path.relative_to(REPO_ROOT).as_posix() for path in markdown_files()}
        assert set(_REQUIRED) <= scanned

    def test_no_tracked_markdown_file_has_a_conflict_marker(self) -> None:
        found = conflict_markers(markdown_files())
        assert not found, "merge-conflict markers left in Markdown files:\n" + "\n".join(found)


class TestConflictMarkers:
    def test_a_file_without_markers_passes(self, tmp_path: Path) -> None:
        clean = tmp_path / "clean.md"
        clean.write_text(
            "# Title\n\n<<<<<< six is not a marker\n======== eight is not one either\n"
            "a line with ======= inside\n> >>>>>>> quoted\n",
            encoding="utf-8",
        )
        assert conflict_markers([clean], root=tmp_path) == []

    @pytest.mark.parametrize(
        "marker",
        [
            "<<<<<<< HEAD",
            "<<<<<<<",
            "=======",
            ">>>>>>> origin/main",
            ">>>>>>> 1234abcd (feat: something)",
            ">>>>>>>",
            "||||||| merged common ancestors",
        ],
    )
    def test_each_marker_is_reported_with_its_file_and_line(self, tmp_path: Path, marker: str) -> None:
        doc = tmp_path / "docs" / "guide.md"
        doc.parent.mkdir()
        doc.write_text(f"# Guide\n\ntext\n{marker}\nmore\n", encoding="utf-8")
        assert conflict_markers([doc], root=tmp_path) == [f"docs/guide.md:4: {marker}"]

    def test_every_marker_of_a_conflict_is_reported_in_order(self, tmp_path: Path) -> None:
        first = tmp_path / "CHANGELOG.md"
        first.write_text("## [1.0.0]\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n", encoding="utf-8")
        second = tmp_path / "README.md"
        second.write_text("# Readme\n", encoding="utf-8")
        assert conflict_markers([first, second], root=tmp_path) == [
            "CHANGELOG.md:2: <<<<<<< HEAD",
            "CHANGELOG.md:4: =======",
            "CHANGELOG.md:6: >>>>>>> branch",
        ]

    def test_crlf_line_endings_are_still_caught(self, tmp_path: Path) -> None:
        doc = tmp_path / "SECURITY.md"
        doc.write_bytes(b"# Security\r\n=======\r\n")
        assert conflict_markers([doc], root=tmp_path) == ["SECURITY.md:2: ======="]
