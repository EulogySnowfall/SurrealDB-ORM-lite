"""Public wording of the read hook (v0.50.0, AC-15 (e), AC-23): no tracked text describes an effect of an undecodable
value on other requests.

Degenerate geometries and out-of-range datetimes are described functionally only: they "are read as in 0.40.x", and
"an undecodable value raises ``SurrealDbConnectionError``". The scan covers the names and the contents of the README,
the guides, the changelog, the security policy, the examples, the sources and the tests, this file excluded (it holds
the phrases it looks for). No server needed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
#: Phrases no public text may hold, compared case-insensitively.
_BANNED = ("every request in flight", "fails other requests", "denial of service")
#: What the scan reads: files at the root, and every file under these folders.
_FILES = ("README.md", "CHANGELOG.md", "SECURITY.md")
_FOLDERS = ("docs", "examples", "src", "tests")
#: Folders of generated files, never published.
_SKIPPED = {"__pycache__", ".ipynb_checkpoints", ".pytest_cache"}


def _scanned() -> list[Path]:
    found = [ROOT / name for name in _FILES]
    for folder in _FOLDERS:
        found += [
            path
            for path in sorted((ROOT / folder).rglob("*"))
            if path.is_file() and not _SKIPPED & set(path.relative_to(ROOT).parts)
        ]
    return [path for path in found if path.resolve() != Path(__file__).resolve()]


def _offences(name: str, text: str) -> list[str]:
    lowered = " ".join(text.lower().split())
    return [f"{name}: {phrase!r}" for phrase in _BANNED if phrase in lowered]


class TestPublicWording:
    def test_the_scan_reads_every_kind_of_file(self) -> None:
        names = {path.relative_to(ROOT).parts[0] for path in _scanned()}
        assert names == {*_FILES, *_FOLDERS}, names
        assert Path(__file__).resolve() not in {path.resolve() for path in _scanned()}

    def test_no_file_name_holds_a_banned_phrase(self) -> None:
        offences = [
            hit for path in _scanned() for hit in _offences(path.relative_to(ROOT).as_posix(), path.name.replace("_", " "))
        ]
        assert offences == []

    def test_no_file_holds_a_banned_phrase(self) -> None:
        offences = []
        for path in _scanned():
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue  # a binary file (an image): no prose
            offences += _offences(path.relative_to(ROOT).as_posix(), text)
        assert offences == []

    @pytest.mark.parametrize("phrase", _BANNED)
    def test_the_check_finds_a_phrase_whatever_its_case_and_line_breaks(self, phrase: str) -> None:
        first, _, rest = phrase.partition(" ")
        assert _offences("x.md", f"It {first.upper()}\n   {rest} too.") == [f"x.md: {phrase!r}"]
