"""Lint tests for the SurrealDB readiness probe used by the workflows.

SurrealDB's ``/health`` answers **200 with an empty body**, so the status code is
the whole signal. Both workflows instead piped the body into ``grep -q "OK"``,
which can never match: every probe ran its full retry budget and then reported a
server that had been up for a minute as unhealthy.

The bug was invisible for months because neither caller acted on the result:

* ``ci.yml`` only ``break``-ed out of the loop, then ran the tests anyway — they
  passed, against the very server the probe had just given up on. The only
  symptom was ~30s of dead time on each of the eight matrix jobs.
* ``dependabot-automerge.yml`` had the same loop until #134 wrapped it in
  ``start_one``, which *returns 1* on timeout. That turned the latent bug into a
  hard failure on the next Dependabot PR (#150), blocking the auto-merge chain.

So this guards both halves — the probe must key on the status code, and it must
fail the step when the server really never comes up.
"""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"

# Workflows that start a SurrealDB container and wait for it.
PROBING_WORKFLOWS = ("ci.yml", "dependabot-automerge.yml")

# The body of /health piped into a matcher, in any order of flags.
_BODY_MATCH = re.compile(r"curl[^\n|]*/health[^\n|]*\|\s*(grep|rg|awk|sed)")

# A status-code probe: curl must be told to fail on a non-2xx response.
_STATUS_PROBE = re.compile(r"curl\s+(-\w+\s+)*-\w*f\w*\b[^\n]*/health")


def _text(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


class TestWorkflowsExist:
    def test_probing_workflows_are_present(self) -> None:
        for name in PROBING_WORKFLOWS:
            assert (WORKFLOWS / name).is_file(), f"{name} not found in {WORKFLOWS}"


class TestHealthProbe:
    def test_probe_never_greps_the_response_body(self) -> None:
        """``/health`` returns no body — matching on it always times out."""
        offenders = [name for name in PROBING_WORKFLOWS if _BODY_MATCH.search(_text(name))]
        assert not offenders, f"health probe matches on the response body in: {', '.join(offenders)}"

    def test_probe_keys_on_the_status_code(self) -> None:
        """``curl -sf`` exits non-zero on a non-2xx response; plain ``-s`` does not."""
        for name in PROBING_WORKFLOWS:
            text = _text(name)
            assert "/health" in text, f"{name} no longer probes /health — update this test"
            assert _STATUS_PROBE.search(text), f"{name} probes /health without curl's --fail"


class TestProbeIsEnforced:
    def test_a_server_that_never_comes_up_fails_the_step(self) -> None:
        """A probe nobody acts on is not a probe.

        ``ci.yml`` used to fall through to the test run after giving up, so a
        genuinely dead server surfaced as a wall of confusing connection errors
        instead of "SurrealDB did not become healthy".
        """
        for name in PROBING_WORKFLOWS:
            text = _text(name)
            assert "did not become healthy" in text, f"{name} does not report a failed probe"
            marker = text.index("did not become healthy")
            tail = text[marker:]
            assert re.search(r"^\s*(exit|return)\s+1\b", tail, re.MULTILINE), (
                f"{name} reports a failed probe but does not fail the step"
            )
