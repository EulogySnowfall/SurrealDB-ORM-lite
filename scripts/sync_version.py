#!/usr/bin/env python3
"""Sync version from pyproject.toml to all version references in the codebase.

Usage:
    python scripts/sync_version.py          # Check only (exit 1 if out of sync)
    python scripts/sync_version.py --fix    # Fix all version references
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

VERSION_LOCATIONS: list[tuple[Path, str]] = [
    (ROOT / "src/surreal_orm_lite/__init__.py", r'__version__ = ".*?"'),
    (ROOT / "tests/test_v050.py", r'assert surreal_orm_lite\.__version__ == ".*?"'),
]


def get_canonical_version() -> str:
    """Read version from pyproject.toml (single source of truth)."""
    pyproject = ROOT / "pyproject.toml"
    match = re.search(r'^version = "(.+?)"', pyproject.read_text(), re.MULTILINE)
    if not match:
        print("ERROR: Could not find version in pyproject.toml")
        sys.exit(1)
    return match.group(1)


def check_and_fix(version: str, *, fix: bool) -> bool:
    """Check/fix all version locations. Returns True if all in sync."""
    replacements: dict[Path, tuple[str, str]] = {
        ROOT / "src/surreal_orm_lite/__init__.py": (
            r'__version__ = ".*?"',
            f'__version__ = "{version}"',
        ),
        ROOT / "tests/test_v050.py": (
            r'assert surreal_orm_lite\.__version__ == ".*?"',
            f'assert surreal_orm_lite.__version__ == "{version}"',
        ),
    }

    all_synced = True

    for filepath, (pattern, replacement) in replacements.items():
        content = filepath.read_text()
        match = re.search(pattern, content)

        if not match:
            print(f"WARNING: Pattern not found in {filepath.relative_to(ROOT)}")
            continue

        if match.group(0) == replacement:
            print(f"  OK  {filepath.relative_to(ROOT)}")
            continue

        all_synced = False
        if fix:
            new_content = re.sub(pattern, replacement, content)
            filepath.write_text(new_content)
            print(f"  FIXED  {filepath.relative_to(ROOT)}: {match.group(0)} -> {replacement}")
        else:
            print(f"  OUT OF SYNC  {filepath.relative_to(ROOT)}: found {match.group(0)}, expected {replacement}")

    return all_synced


def main() -> None:
    fix = "--fix" in sys.argv
    version = get_canonical_version()

    print(f"Canonical version (pyproject.toml): {version}")
    print()

    all_synced = check_and_fix(version, fix=fix)

    if not all_synced and not fix:
        print()
        print("Version mismatch detected. Run 'make sync-version' to fix.")
        sys.exit(1)

    if all_synced:
        print()
        print("All versions are in sync.")


if __name__ == "__main__":
    main()
