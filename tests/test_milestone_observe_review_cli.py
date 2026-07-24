"""ms-119 (AX + 保守性 review of PR #487): the `--review` flag on
`beacon milestone observe` must be parsed at the SHELL entrypoint (bin/beacon),
not only in the Python handler.

Background: the first cut of this feature added a `BEACON_REVIEW=1` branch to the
Python `cmd_milestone_observe`, and the refusal error + all SKILL docs told the
AI to run `beacon milestone observe <id> --review`. But `bin/beacon` had no
`--review` arm for observe, so the real CLI rejected the flag with a generic
invalid-flag error — the advertised recovery path was dead. The unit tests set
`BEACON_REVIEW` directly on the Python function and so jumped over the bash flag
parser (a false green). Two independent reviewers (AX + maintainability) caught
it. These tests exercise the REAL CLI path so the bash↔python seam is guarded.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

BEACON = Path(__file__).resolve().parent.parent / "bin" / "beacon"


@pytest.fixture
def local_project(tmp_path):
    """A local-mode project (project.json, no cloud.json) with one in_progress
    milestone — in_progress is a work state, so -> observing is a completion
    claim (gated)."""
    beacon = tmp_path / ".beacon"
    beacon.mkdir()
    (beacon / "project.json").write_text(json.dumps({
        "name": "ObserveReviewCLI",
        "summary": "",
        "milestones": [{
            "id": "ms-1", "title": "p", "status": "in_progress",
            "target_date": "", "commits": [], "entries": [],
        }],
    }))
    return tmp_path


def _run(cwd, *args, env_extra=None):
    env = dict(os.environ)
    env.pop("BEACON_REVIEW", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(BEACON), *args],
        capture_output=True, text=True, cwd=str(cwd), env=env, timeout=30,
    )


def _status(project_dir):
    d = json.loads((project_dir / ".beacon" / "project.json").read_text())
    return d["milestones"][0]["status"]


def test_observe_review_flag_parsed_and_routes_at_cli(local_project):
    """The exact recovery path the refusal advertises: an AI session running
    `observe <id> --review` must be accepted by the shell and routed to the
    目的達成 gate (pending approval, status unchanged) — NOT rejected as an
    invalid flag, NOT blocked by the e-4008 ban (which --review bypasses)."""
    r = _run(local_project, "milestone", "observe", "ms-1",
             "--reason", "運用移行", "--review",
             env_extra={"BEACON_SESSION_KIND": "claude"})
    combined = r.stdout + r.stderr
    assert "is not a valid flag" not in combined, combined
    assert "e-4008" not in combined, combined  # --review is the sanctioned route
    assert "承認待ち" in r.stdout, combined      # routed to human approval
    assert _status(local_project) == "in_progress"  # not applied directly


def test_observe_bare_ai_session_refused_at_cli(local_project):
    """A bare (no --review) AI-session observe of a work-state milestone must be
    refused through the real CLI (e-4008 ban), closing the gate-bypass hole."""
    r = _run(local_project, "milestone", "observe", "ms-1", "--reason", "x",
             env_extra={"BEACON_SESSION_KIND": "claude"})
    combined = r.stdout + r.stderr
    assert "e-4008" in combined, combined
    assert _status(local_project) == "in_progress"


def test_observe_human_direct_applies_at_cli(local_project):
    """A human session keeps the straight path: bare observe applies directly."""
    r = _run(local_project, "milestone", "observe", "ms-1",
             "--reason", "運用移行",
             env_extra={"BEACON_SESSION_KIND": "human"})
    assert _status(local_project) == "observing", r.stdout + r.stderr
