#!/usr/bin/env python3
"""Sync the SurrealDB server pins to every place that restates them.

The pin files are the single source of truth:

    .surrealdb-version      the primary supported line (3.2.x)
    .surrealdb-version2x    the legacy supported line  (2.6.x)

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

# Deliberately NOT covered: CHANGELOG.md. Its entries state what a given release was cut and
# verified against, which stays true after a later bump — rewriting them would falsify the
# history. Same for docs/superpowers/, which is dated design material.
TARGETS = ["README.md", "docs/ROADMAP.md", ".github/workflows/ci.yml"]


def read_pin(path: Path) -> str:
    if not path.exists():
        sys.exit(f"ERROR: missing pin file {path.relative_to(ROOT)}")
    version = path.read_text().strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        sys.exit(f"ERROR: {path.relative_to(ROOT)} holds '{version}', expected X.Y.Z")
    return version


def rules(primary: str, legacy: str) -> list[tuple[str, str, str]]:
    """(description, regex, replacement) — each anchored so it can only match a pin restatement.

    Every pattern captures the surrounding literal text and rewrites just the version, so a
    rule can never rewrite an unrelated version-shaped string elsewhere in the file.
    """
    p_major, l_major = primary.split(".")[0], legacy.split(".")[0]
    return [
        (
            "README badge",
            r"(badge/SurrealDB-)\d+\.\d+\.\d+(%20%7C%20)\d+\.\d+\.\d+(-purple)",
            rf"\g<1>{legacy}\g<2>{primary}\g<3>",
        ),
        (
            "README 'tested against' note",
            r"(It is tested against SurrealDB \*\*v)\d+\.\d+\.\d+(\*\* and \*\*v)\d+\.\d+\.\d+(\*\*)",
            rf"\g<1>{legacy}\g<2>{primary}\g<3>",
        ),
        (
            "README function-catalog coverage",
            r"(member is tested against SurrealDB )\d+\.\d+\.\d+( and )\d+\.\d+\.\d+",
            rf"\g<1>{legacy}\g<2>{primary}",
        ),
        (
            "README compatibility table (primary)",
            rf"(\|\s*){p_major}\.\d+\.\d+(\s*\|\s*2\.0\s*\|\s*✅ Tested)",
            rf"\g<1>{primary}\g<2>",
        ),
        (
            "README compatibility table (legacy)",
            rf"(\|\s*){l_major}\.\d+\.\d+(\s*\|\s*2\.0\s*\|\s*✅ Tested)",
            rf"\g<1>{legacy}\g<2>",
        ),
        # Two columns, one per line — anchor on the major or the primary rule rewrites the
        # 2.6.x cell and silently corrupts the table.
        (
            "README behaviour table (primary column)",
            rf"(catalogued member verified on ){p_major}\.\d+\.\d+",
            rf"\g<1>{primary}",
        ),
        (
            "README behaviour table (legacy column)",
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
        (
            "ci.yml matrix",
            r"(surrealdb-version: \['v)\d+\.\d+\.\d+(', 'v)\d+\.\d+\.\d+('\])",
            rf"\g<1>{legacy}\g<2>{primary}\g<3>",
        ),
    ]


def main() -> None:
    fix = "--fix" in sys.argv
    primary, legacy = read_pin(PRIMARY_PIN), read_pin(LEGACY_PIN)

    print(f"Pins: primary={primary} ({PRIMARY_PIN.name})  legacy={legacy} ({LEGACY_PIN.name})")
    print()

    all_rules = rules(primary, legacy)
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
