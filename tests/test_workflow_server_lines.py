"""Lint tests for the three supported SurrealDB server lines in the workflows (no database needed).

v0.50.0 adds SurrealDB 4.x, pinned in ``.surrealdb-version4x``, beside 2.x (``.surrealdb-version2x``) and 3.x
(``.surrealdb-version``). Every workflow that starts a server must start each line, and the 4.x line needs its own
daily monitor — otherwise its patches go unnoticed the way 3.2.1 to 3.2.4 did before the 3.x monitor existed.

Text, not a YAML parse, to match ``test_workflow_health_probe.py``. Where a check is about shell behaviour (the
versioned store chosen per line, the monitor's matrix rewrite), the shell itself is run on the extracted lines.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"

#: Pin file of each line, oldest line first (the order of the CI matrix).
PIN_FILES = {"2": ".surrealdb-version2x", "3": ".surrealdb-version", "4": ".surrealdb-version4x"}

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash to run the workflow shell")


def _text(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def _pin(line: str) -> str:
    return (ROOT / PIN_FILES[line]).read_text(encoding="utf-8").strip()


def _job(workflow: str, job: str) -> str:
    """The block of *job* in *workflow*: from its key to the next top-level job."""
    text = _text(workflow)
    match = re.search(rf"^  {re.escape(job)}:$", text, re.MULTILINE)
    assert match, f"job {job} is gone from {workflow} — update this test"
    block = text[match.end() :]
    following = re.search(r"^  [a-z][a-z0-9-]*:$", block, re.MULTILINE)
    return block[: following.start()] if following else block


class TestCiMatrix:
    def test_the_matrix_holds_the_three_pins_oldest_first(self) -> None:
        expected = ", ".join(f"'v{_pin(line)}'" for line in ("2", "3", "4"))
        assert f"surrealdb-version: [{expected}]" in _text("ci.yml")


class TestVersionedServer:
    """``tests/test_time_travel.py`` reads history from a second server started with versioned storage."""

    @staticmethod
    def _store_for(tag: str) -> subprocess.CompletedProcess[str]:
        text = _text("ci.yml")
        start = text.index('case "${{ matrix.surrealdb-version }}" in')
        end = text.index("esac", start) + len("esac")
        script = text[start:end].replace("${{ matrix.surrealdb-version }}", "$1")
        return subprocess.run(
            ["bash", "-c", f'set -e\n{script}\necho "$store"', "_", tag],
            capture_output=True,
            text=True,
            timeout=30,
        )

    @needs_bash
    @pytest.mark.parametrize(
        ("line", "store"),
        [
            ("2", "surrealkv+versioned:///data/db"),
            ("3", "surrealkv:///data/db?versioned=true"),
            ("4", "surrealkv:///data/db?versioned=true"),
        ],
    )
    def test_each_line_gets_its_versioned_store(self, line: str, store: str) -> None:
        result = self._store_for(f"v{_pin(line)}")
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == store

    @needs_bash
    def test_an_unknown_line_fails_instead_of_guessing_a_store(self) -> None:
        """A fifth line must be added on purpose: a store URL that line does not understand fails late and obscurely."""
        result = self._store_for("v5.0.0")
        assert result.returncode != 0
        assert "v5.0.0" in result.stderr


class TestMixedLinesJob:
    def test_it_starts_the_three_pinned_lines(self) -> None:
        block = _job("ci.yml", "mixed-lines")
        for pin_file in PIN_FILES.values():
            assert f"$(cat {pin_file})" in block, f"mixed-lines does not start the {pin_file} line"

    def test_it_names_each_line_by_its_port(self) -> None:
        block = _job("ci.yml", "mixed-lines")
        ports = {line: re.search(rf"SURREALDB_PORT_{line}X: (\d+)", block) for line in PIN_FILES}
        assert all(ports.values()), ports
        values = {line: match.group(1) for line, match in ports.items() if match}
        assert len(set(values.values())) == 3, values
        for line, port in values.items():
            assert re.search(
                rf"-p {port}:8000 \\\n\s*\"surrealdb/surrealdb:v\$\(cat {re.escape(PIN_FILES[line])}\)\"", block
            ), f"SURREALDB_PORT_{line}X={port} is not the port the {line}.x server is published on"


class TestDependabotAutomerge:
    def test_it_tests_the_three_pinned_lines(self) -> None:
        block = _job("dependabot-automerge.yml", "test-dependabot")
        for line, pin_file in PIN_FILES.items():
            start = re.search(
                rf"start_one surrealdb-{line}x \"\$\(tr -d '\[:space:\]' < {re.escape(pin_file)}\)\"\s+(\d+)", block
            )
            assert start, f"test-dependabot does not start the {line}.x line"
            port = start.group(1)
            assert re.search(rf"SURREALDB_PORT: {port}\n\s*run: (\|\s*)?uv run pytest tests/", block), (
                f"test-dependabot starts the {line}.x line on :{port} but runs no suite against it"
            )
            assert f"surrealdb-{line}x" in block.split("docker rm -f", 1)[1].splitlines()[0]


class TestFourXMonitor:
    CALLER = "surrealdb-4x-monitor.yml"

    def test_it_calls_the_reusable_monitor_for_line_4(self) -> None:
        text = _text(self.CALLER)
        assert "uses: ./.github/workflows/surrealdb-monitor.yml" in text
        assert "line: '4'" in text
        assert "pin-file: .surrealdb-version4x" in text
        assert "version-source: dockerhub" in text

    def test_it_runs_daily_apart_from_the_other_monitors(self) -> None:
        crons = {
            name: re.findall(r"cron: '([^']+)'", _text(name))
            for name in (self.CALLER, "surrealdb-security.yml", "surrealdb-3x-monitor.yml")
        }
        assert all(len(found) == 1 for found in crons.values()), crons
        assert len({found[0] for found in crons.values()}) == 3, crons
        assert crons[self.CALLER][0].endswith(" * * *"), crons[self.CALLER]

    def test_the_reusable_monitor_documents_line_4(self) -> None:
        assert '"4"' in _text("surrealdb-monitor.yml").split("line:", 1)[1].split("type:", 1)[0]

    @needs_bash
    def test_the_matrix_rewrite_touches_only_the_4x_entry(self, tmp_path: Path) -> None:
        """The monitor's ``sed`` on a copy of the real ``ci.yml``: a 4.x bump leaves the 2.x and 3.x entries alone."""
        (command,) = [
            line.strip()
            for line in _text("surrealdb-monitor.yml").splitlines()
            if line.strip().startswith("sed -i -E \"s/'v${LINE}")
        ]
        copy = tmp_path / ".github" / "workflows" / "ci.yml"
        copy.parent.mkdir(parents=True)
        copy.write_text(_text("ci.yml"), encoding="utf-8")
        result = subprocess.run(
            ["bash", "-c", command],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=tmp_path,
            env={"LINE": "4", "DOCKER_TAG": "v4.9.9", "PATH": "/usr/bin:/bin"},
        )
        assert result.returncode == 0, result.stderr
        expected = f"surrealdb-version: ['v{_pin('2')}', 'v{_pin('3')}', 'v4.9.9']"
        assert expected in copy.read_text(encoding="utf-8")
