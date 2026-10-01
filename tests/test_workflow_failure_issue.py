"""Lint tests for the monitor's "tests failed" issue — the only thing it reports with.

``gh issue create`` validates labels server-side and refuses the **whole** call when one
does not exist. The step asked for ``bug,surrealdb,needs-investigation`` and this repo has
only the first two, so every SurrealDB 3.x failure since v3.3.0 ended the same way::

    could not add label: 'needs-investigation' not found
    ##[error]Process completed with exit code 1.

The run went red and the compatibility failure it exists to surface was never filed — the
same silent-reporting failure as #146 (the job was unreachable) arriving by a second route,
which is why it is worth pinning rather than just fixing.

So this guards both halves: the labels must be created before they are used, and filing the
report must not depend on labelling succeeding at all.

Text, not a YAML parse, to match ``test_workflow_health_probe.py`` — the assertions are all
about the shell inside the step, and a parser would only add a dependency to reach it.
"""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "surrealdb-monitor.yml"

FAILURE_JOB = "create-failure-issue"

# The job runs last in the file, so its block is "from its key to the end".
_JOB_START = re.compile(rf"^  {FAILURE_JOB}:$", re.MULTILINE)


def _job_block() -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    match = _JOB_START.search(text)
    assert match, f"{FAILURE_JOB} is gone from {WORKFLOW.name} — update this test"

    block = text[match.end() :]
    # Stop at the next top-level job, if one is ever added below it.
    following = re.search(r"^  [a-z][a-z0-9-]*:$", block, re.MULTILINE)
    block = block[: following.start()] if following else block

    # Drop comment lines. Every assertion below is about the ORDER of real commands, and
    # the comments here quote the commands they explain — scanning those as if they were
    # code puts `gh issue create` before the `gh label create` that actually guards it.
    return "\n".join(line for line in block.splitlines() if not line.lstrip().startswith("#"))


class TestFailureIssueJobExists:
    def test_the_monitor_still_files_an_issue_on_failure(self) -> None:
        assert "gh issue create" in _job_block()


class TestLabelsCannotSwallowTheReport:
    def test_every_label_it_asks_for_is_created_first(self) -> None:
        """A label the repo does not have must be created, not assumed."""
        block = _job_block()

        assert "gh label create" in block, (
            "the step passes --label without creating the labels first; a label nobody "
            "created makes gh reject the whole issue and the failure goes unreported"
        )
        assert block.index("gh label create") < block.index("gh issue create"), (
            "labels are created after the issue — too late to help it"
        )

    def test_label_creation_does_not_clobber_an_existing_label(self) -> None:
        """``--force`` would overwrite a curated label's colour and description."""
        for line in _job_block().splitlines():
            if "gh label create" in line:
                assert "--force" not in line, f"gh label create must not use --force: {line}"

    def test_a_labelling_failure_still_files_the_report(self) -> None:
        """Belt and braces: an unlabelled issue beats a red run and silence."""
        block = _job_block()

        assert block.count("gh issue create") >= 2, (
            "there is no label-free fallback — if labelling fails for any reason "
            "(permissions, a rename, a race) the report is lost"
        )
        fallback = block[block.index("gh issue create") :]
        fallback = fallback[fallback.index("gh issue create", 1) :]
        assert not re.search(r"^\s*--label", fallback, re.MULTILINE), (
            "the fallback issue create also passes --label, so it fails the same way"
        )

    def test_the_body_is_built_once_and_reused(self) -> None:
        """Both create calls must report the same thing."""
        assert "ISSUE_BODY=" in _job_block()
