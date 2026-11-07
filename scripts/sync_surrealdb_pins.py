#!/usr/bin/env python3
"""Sync the SurrealDB server pins to every place that restates them.

The pin files are the single source of truth:

    .surrealdb-version      the primary supported line (3.x)
    .surrealdb-version2x    the legacy supported line  (2.x)
    .surrealdb-version4x    the newest supported line  (4.x, since v0.50.0)

The version monitors bump those files unattended and open an auto-merging PR, but by design
they touch only the pin and the CI matrix — never the prose. So every automatic bump used to
leave the README advertising the previous version, starting with the badge a reader sees
first. This turns that drift into a CI failure, and `--fix` into a one-liner the monitor can
run so the docs travel in the same PR as the bump.

Usage:
    python scripts/sync_surrealdb_pins.py          # Check only (exit 1 if out of sync)
    python scripts/sync_surrealdb_pins.py --fix    # Rewrite every reference from the pins
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PRIMARY_PIN = ROOT / ".surrealdb-version"
LEGACY_PIN = ROOT / ".surrealdb-version2x"
LATEST_PIN = ROOT / ".surrealdb-version4x"

# Deliberately NOT covered: CHANGELOG.md. Its entries state what a given release was cut and
# verified against, which stays true after a later bump — rewriting them would falsify the
# history. The dated design specs and plans are out of reach for the same reason: they live
# with the maintainer's working notes, outside the repository.
# The guides under docs/ restate the pins too (the compatibility tables, the "tested against" note): every one of them
# is read, so a sentence moved from one guide to another stays covered.
TARGETS = [
    "README.md",
    *sorted(path.relative_to(ROOT).as_posix() for path in (ROOT / "docs").glob("*.md")),
    ".github/workflows/ci.yml",
]


def read_pin(path: Path) -> str:
    if not path.exists():
        sys.exit(f"ERROR: missing pin file {path.relative_to(ROOT)}")
    version = path.read_text().strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        sys.exit(f"ERROR: {path.relative_to(ROOT)} holds '{version}', expected X.Y.Z")
    return version


def rules(primary: str, legacy: str, latest: str) -> list[tuple[str, str, str]]:
    """(description, regex, replacement) — each anchored so it can only match a pin restatement.

    Every pattern captures the surrounding literal text and rewrites just the version, so a
    rule can never rewrite an unrelated version-shaped string elsewhere in the file.
    """
    p_major, l_major, n_major = primary.split(".")[0], legacy.split(".")[0], latest.split(".")[0]
    return [
        # The badge and the "tested against" sentence name the three lines, oldest first (since v0.50.0).
        (
            "README badge",
            r"(badge/SurrealDB-)\d+\.\d+\.\d+(%20%7C%20)\d+\.\d+\.\d+(%20%7C%20)\d+\.\d+\.\d+(-purple)",
            rf"\g<1>{legacy}\g<2>{primary}\g<3>{latest}\g<4>",
        ),
        (
            "'tested against' note",
            r"(It is tested against SurrealDB \*\*v)\d+\.\d+\.\d+(\*\*, \*\*v)\d+\.\d+\.\d+(\*\* and \*\*v)"
            r"\d+\.\d+\.\d+(\*\*)",
            rf"\g<1>{legacy}\g<2>{primary}\g<3>{latest}\g<4>",
        ),
        (
            "function-catalog coverage",
            r"(member is tested against SurrealDB )\d+\.\d+\.\d+( and )\d+\.\d+\.\d+",
            rf"\g<1>{legacy}\g<2>{primary}",
        ),
        # One row per line, each on SDK 3.0 (since v0.50.0).
        (
            "compatibility table (primary)",
            rf"(\|\s*){p_major}\.\d+\.\d+(\s*\|\s*3\.0\s*\|\s*✅ Tested)",
            rf"\g<1>{primary}\g<2>",
        ),
        (
            "compatibility table (legacy)",
            rf"(\|\s*){l_major}\.\d+\.\d+(\s*\|\s*3\.0\s*\|\s*✅ Tested)",
            rf"\g<1>{legacy}\g<2>",
        ),
        (
            "compatibility table (latest)",
            rf"(\|\s*){n_major}\.\d+\.\d+(\s*\|\s*3\.0\s*\|\s*✅ Tested)",
            rf"\g<1>{latest}\g<2>",
        ),
        # Two columns, one per line — anchor on the major or the primary rule rewrites the
        # 2.6.x cell and silently corrupts the table.
        (
            "behaviour table (primary column)",
            rf"(catalogued member verified on ){p_major}\.\d+\.\d+",
            rf"\g<1>{primary}",
        ),
        (
            "behaviour table (legacy column)",
            rf"(catalogued member verified on ){l_major}\.\d+\.\d+",
            rf"\g<1>{legacy}",
        ),
        (
            "ROADMAP CI matrix",
            r"(CI matrix tested on SurrealDB \*\*v)\d+\.\d+\.\d+( and v)\d+\.\d+\.\d+(\*\*)",
            rf"\g<1>{legacy}\g<2>{primary}\g<3>",
        ),
        (
            "ROADMAP suite coverage",
            r"(member is executed against )\d+\.\d+\.\d+( AND )\d+\.\d+\.\d+( by the suite)",
            rf"\g<1>{legacy}\g<2>{primary}\g<3>",
        ),
        # One entry per line, oldest first. The optional third entry also matches the two-line matrix of
        # the releases before 0.50, so --fix adds the 4.x line instead of failing on an unmatched rule.
        (
            "ci.yml matrix",
            r"(surrealdb-version: \[)'v\d+\.\d+\.\d+', 'v\d+\.\d+\.\d+'(?:, 'v\d+\.\d+\.\d+')?(\])",
            rf"\g<1>'v{legacy}', 'v{primary}', 'v{latest}'\g<2>",
        ),
    ]


def main() -> None:
    fix = "--fix" in sys.argv
    primary, legacy, latest = read_pin(PRIMARY_PIN), read_pin(LEGACY_PIN), read_pin(LATEST_PIN)

    print(
        f"Pins: primary={primary} ({PRIMARY_PIN.name})  legacy={legacy} ({LEGACY_PIN.name})"
        f"  latest={latest} ({LATEST_PIN.name})"
    )
    print()

    all_rules = rules(primary, legacy, latest)
    unmatched = [name for name, pattern, _ in all_rules]
    in_sync = True

    for target in TARGETS:
        path = ROOT / target
        original = path.read_text()
        content = original

        for name, pattern, replacement in all_rules:
            match = re.search(pattern, content)
            if not match:
                continue
            if name in unmatched:  # a sentence may be restated in several files (README and a guide)
                unmatched.remove(name)
            updated = re.sub(pattern, replacement, content)
            if updated == content:
                print(f"  OK  {target}: {name}")
                continue
            in_sync = False
            if fix:
                content = updated
                print(f"  FIXED  {target}: {name} — was {match.group(0)!r}")
            else:
                print(f"  OUT OF SYNC  {target}: {name} — found {match.group(0)!r}")

        if fix and content != original:
            path.write_text(content)

    # A rule that matches nothing is a silent hole: the prose was reworded and this guard
    # stopped watching it. Louder than a mismatch, because it fails open.
    if unmatched:
        print()
        for name in unmatched:
            print(f"  ERROR  rule '{name}' matched nothing — the text moved or was reworded.")
        print("Update scripts/sync_surrealdb_pins.py to match the new wording.")
        sys.exit(1)

    print()
    if in_sync:
        print("Every SurrealDB version reference matches the pins.")
    elif fix:
        print("References rewritten from the pins.")
    else:
        print("Drift detected. Run: python scripts/sync_surrealdb_pins.py --fix")
        sys.exit(1)


if __name__ == "__main__":
    main()
