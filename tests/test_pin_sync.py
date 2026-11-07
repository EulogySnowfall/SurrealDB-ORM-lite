"""The SurrealDB server pins and ``scripts/sync_surrealdb_pins.py`` (no database needed).

v0.50.0 adds a third supported line, SurrealDB 4.x, pinned in ``.surrealdb-version4x``: the drift check reads it and
rewrites its restatement in the CI matrix.
"""

import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "sync_surrealdb_pins.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sync_surrealdb_pins", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_each_line_has_its_pin_file() -> None:
    pins = {
        ".surrealdb-version2x": "2",
        ".surrealdb-version": "3",
        ".surrealdb-version4x": "4",
    }
    for name, major in pins.items():
        version = (ROOT / name).read_text().strip()
        assert re.fullmatch(r"\d+\.\d+\.\d+", version), (name, version)
        assert version.split(".")[0] == major, (name, version)


def test_the_script_reads_the_4x_pin() -> None:
    script = _load_script()
    assert script.LATEST_PIN == ROOT / ".surrealdb-version4x"


def test_ci_matrix_rule_lists_the_three_lines() -> None:
    """The CI matrix holds one entry per line; a two-entry matrix (before 0.50) is rewritten to three."""
    script = _load_script()
    (rule,) = [r for r in script.rules("3.3.5", "2.7.0", "4.0.0") if r[0] == "ci.yml matrix"]
    _, pattern, replacement = rule
    expected = "surrealdb-version: ['v2.7.0', 'v3.3.5', 'v4.0.0']"
    for before in (
        "surrealdb-version: ['v2.6.0', 'v3.3.0']",
        "surrealdb-version: ['v2.6.0', 'v3.3.0', 'v4.0.1']",
        expected,
    ):
        assert re.sub(pattern, replacement, before) == expected, before


def test_repository_is_in_sync_with_the_pins() -> None:
    result = subprocess.run(
        [sys.executable, "-I", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "v4" in (ROOT / ".github" / "workflows" / "ci.yml").read_text().split("surrealdb-version: [")[1].split("]")[0]


def _rule(name: str) -> tuple[str, str]:
    script = _load_script()
    (rule,) = [r for r in script.rules("3.3.5", "2.7.0", "4.0.0") if r[0] == name]
    return rule[1], rule[2]


def test_the_badge_and_the_tested_note_name_the_three_lines() -> None:
    """v0.50.0: the README badge and the "tested against" sentence restate the three pins, oldest line first."""
    pattern, replacement = _rule("README badge")
    badge = "badge/SurrealDB-2.6.0%20%7C%203.3.0%20%7C%204.0.1-purple"
    assert re.sub(pattern, replacement, badge) == "badge/SurrealDB-2.7.0%20%7C%203.3.5%20%7C%204.0.0-purple"
    pattern, replacement = _rule("'tested against' note")
    note = "It is tested against SurrealDB **v2.6.0**, **v3.3.0** and **v4.0.1**"
    assert re.sub(pattern, replacement, note) == "It is tested against SurrealDB **v2.7.0**, **v3.3.5** and **v4.0.0**"


def test_the_compatibility_table_rules_cover_the_three_lines_on_sdk_3() -> None:
    """The tested-versions table of ``docs/compatibility.md`` has one SDK 3.0 row per line, each rewritten from its pin."""
    table = "| 3.3.0 | 3.0 | ✅ Tested |\n| 2.6.0 | 3.0 | ✅ Tested |\n| 4.0.1 | 3.0 | ✅ Tested |\n"
    for name in ("compatibility table (primary)", "compatibility table (legacy)", "compatibility table (latest)"):
        pattern, replacement = _rule(name)
        table = re.sub(pattern, replacement, table)
    assert table == "| 3.3.5 | 3.0 | ✅ Tested |\n| 2.7.0 | 3.0 | ✅ Tested |\n| 4.0.0 | 3.0 | ✅ Tested |\n"
