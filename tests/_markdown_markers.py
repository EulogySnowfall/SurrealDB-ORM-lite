"""Find merge-conflict markers left in the repository's Markdown files (issue #398).

Used by ``tests/test_markdown_conflict_markers.py``. :func:`markdown_files` lists the tracked ``*.md`` files (``git
ls-files``), or, without a git checkout, the documentation files the release process edits; :func:`conflict_markers`
returns a ``file:line: text`` entry for every line that is a conflict marker.

A marker is matched as git writes it: seven ``<``, ``>`` or ``|`` alone or followed by a space and a label, and seven
``=`` alone on the line. Six or eight characters, text before the marker or after ``=======`` do not match, which
keeps out ordinary text and longer setext underlines. A setext underline of exactly seven ``=`` would still match:
the documentation has none, and a longer or shorter one renders the same.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Without git, the files the documentation tests and the release process care about.
_FALLBACK = ("CHANGELOG.md", "README.md", "SECURITY.md")

_MARKER = re.compile(r"^(?:(?:<{7}|>{7}|\|{7})(?: .*)?|={7})$")


def markdown_files(root: Path = REPO_ROOT) -> list[Path]:
    """The tracked Markdown files under ``root``, sorted; the known documentation files when git is unavailable."""
    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z", "--", "*.md"],
            cwd=root,
            capture_output=True,
            check=True,
        ).stdout.decode("utf-8")
    except (OSError, subprocess.CalledProcessError):
        candidates = [root / name for name in _FALLBACK] + list((root / "docs").rglob("*.md"))
    else:
        candidates = [root / name for name in listed.split("\0") if name]
    return sorted(path for path in candidates if path.is_file())


def conflict_markers(paths: Iterable[Path], root: Path = REPO_ROOT) -> list[str]:
    """``file:line: text`` for every conflict-marker line of ``paths``, file paths relative to ``root``."""
    found: list[str] = []
    for path in paths:
        name = path.relative_to(root).as_posix()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _MARKER.match(line):
                found.append(f"{name}:{number}: {line}")
    return found
