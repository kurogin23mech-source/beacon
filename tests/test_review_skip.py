"""Human-intended review skip is first-class and audited (ms-119 / e-4124).

A skip is a governance decision ("a human waived this review, here's why"),
distinct from BEACON_PR_REVIEW_OVERRIDE (force a merge past a review still owed).
It must: require a reason, write a durable audit record, clear the review-due
trigger so the gate proceeds, and leave a grep-able footprint of an AI-recorded
skip (per the e-4006 "can't hide it" pattern).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BEACON = ROOT / "bin" / "beacon"
LIB = ROOT / "lib"
sys.path.insert(0, str(LIB))
import review_spine  # noqa: E402


# ---- pure builder ----------------------------------------------------------

def test_build_review_skip_record_shape():
    rec = review_spine.build_review_skip_record(
        review_type="ax", ref_kind="pr", ref="42", reason="doc-only change",
        actor="mac/claude", gate={"signal": "human-session", "session_kind": "human"},
        at="2026-07-24T00:00:00")
    assert rec["kind"] == "review-skip"
    assert rec["review_type"] == "ax"
    assert rec["ref_kind"] == "pr" and rec["ref"] == "42"
    assert rec["reason"] == "doc-only change"
    assert rec["gate"]["signal"] == "human-session"


# ---- CLI end-to-end --------------------------------------------------------

@pytest.fixture
def proj(tmp_path):
    beacon = tmp_path / ".beacon"
    beacon.mkdir()
    (beacon / "project.json").write_text(json.dumps(
        {"name": "ReviewSkipTest", "summary": "", "milestones": []}))
    return tmp_path


def _run(proj_dir, args, env_extra=None):
    env = dict(os.environ)
    env["BEACON_PROJECT_FILE"] = str(proj_dir / ".beacon" / "project.json")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(BEACON), *args],
        capture_output=True, text=True, cwd=str(proj_dir), timeout=30, env=env)


def _seed_pr_review_due(proj_dir, review_type, pr):
    trig = proj_dir / ".beacon" / "triggers"
    trig.mkdir(parents=True, exist_ok=True)
    (trig / f"{review_type}-review-due-{pr}.json").write_text(json.dumps(
        {"name": f"{review_type}-review-due-{pr}", "review": review_type,
         "pr_number": pr}))


def test_skip_requires_reason(proj):
    _seed_pr_review_due(proj, "ax", "42")
    r = _run(proj, ["review", "skip", "--type", "ax", "--pr", "42"],
             {"BEACON_SESSION_KIND": "human"})
    assert r.returncode != 0
    assert "--reason" in (r.stderr + r.stdout)


def test_skip_records_audit_and_clears_trigger(proj):
    _seed_pr_review_due(proj, "ax", "42")
    r = _run(proj, ["review", "skip", "--type", "ax", "--pr", "42",
                    "--reason", "docs-only PR, no interface change"],
             {"BEACON_SESSION_KIND": "human"})
    assert r.returncode == 0, r.stderr
    # trigger cleared → gate no longer blocks
    assert not (proj / ".beacon" / "triggers" / "ax-review-due-42.json").exists()
    # durable audit written
    log = proj / ".beacon" / "review-skips.jsonl"
    assert log.exists()
    rec = json.loads(log.read_text().strip())
    assert rec["review_type"] == "ax" and rec["ref"] == "42"
    assert rec["reason"] == "docs-only PR, no interface change"
    assert rec["gate"]["signal"] == "human-session"


def test_ai_session_skip_leaves_grep_able_footprint(proj):
    """An AI session (no human signal) can still record a skip, but the audit
    marks it ai-session-unguarded — it cannot look identical to a human's."""
    _seed_pr_review_due(proj, "maintainability", "7")
    r = _run(proj, ["review", "skip", "--type", "maintainability", "--pr", "7",
                    "--reason", "trivial"],
             {"BEACON_SESSION_KIND": ""})
    assert r.returncode == 0, r.stderr
    rec = json.loads((proj / ".beacon" / "review-skips.jsonl").read_text().strip())
    assert rec["gate"]["signal"] == "ai-session-unguarded"
    assert "ai-session-unguarded" in r.stderr


def test_user_override_signal_recorded(proj):
    _seed_pr_review_due(proj, "ax", "9")
    r = _run(proj, ["review", "skip", "--type", "ax", "--pr", "9",
                    "--reason", "user waived it"],
             {"BEACON_SESSION_KIND": "", "BEACON_REVIEW_SKIP_USER_OVERRIDE": "1"})
    assert r.returncode == 0, r.stderr
    rec = json.loads((proj / ".beacon" / "review-skips.jsonl").read_text().strip())
    assert rec["gate"]["signal"] == "user-override"


def test_pr_and_target_mutually_exclusive(proj):
    r = _run(proj, ["review", "skip", "--type", "ax", "--pr", "1",
                    "--target", "ms-1", "--reason", "x"],
             {"BEACON_SESSION_KIND": "human"})
    assert r.returncode != 0
    assert "--pr" in r.stderr and "--target" in r.stderr


# --- e-4124 AX/maint review fixes -------------------------------------------

def test_unknown_type_is_rejected(proj):
    """A typo'd review type must be rejected — otherwise it clears nothing yet
    prints success (silent no-op in the very command meant to make skips visible)."""
    _seed_pr_review_due(proj, "ax", "42")
    r = _run(proj, ["review", "skip", "--type", "maintainabilty", "--pr", "42",
                    "--reason", "typo"], {"BEACON_SESSION_KIND": "human"})
    assert r.returncode != 0
    assert "未知の review type" in r.stderr
    # the real trigger is untouched.
    assert (proj / ".beacon" / "triggers" / "ax-review-due-42.json").exists()


def test_skip_of_non_owed_review_warns_and_exits_nonzero(proj):
    """Skipping a review that is not owed (wrong PR) records the waiver but must
    NOT look like it cleared an obligation."""
    r = _run(proj, ["review", "skip", "--type", "ax", "--pr", "999",
                    "--reason", "nothing here"], {"BEACON_SESSION_KIND": "human"})
    assert r.returncode == 3
    assert "一致する review-due が見つかりません" in r.stderr
    rec = json.loads((proj / ".beacon" / "review-skips.jsonl").read_text().strip())
    assert rec["waived_obligation"] is False


def _seed_target_review_due(proj_dir, target_id, bindings):
    trig = proj_dir / ".beacon" / "triggers"
    trig.mkdir(parents=True, exist_ok=True)
    (trig / f"review-due-{target_id}.json").write_text(json.dumps(
        {"name": f"review-due-{target_id}", "bindings": list(bindings),
         "target_id": target_id}))


def test_per_type_target_skip_preserves_other_bindings(proj):
    """A per-type skip on a target must remove ONLY that type's binding — the
    target review-due file carries all bindings (philosophy + attainment), so
    deleting the whole file would silently waive attainment too."""
    _seed_target_review_due(proj, "ms-1", ["philosophy", "attainment"])
    r = _run(proj, ["review", "skip", "--type", "philosophy", "--target", "ms-1",
                    "--reason", "no spec drift possible"],
             {"BEACON_SESSION_KIND": "human"})
    assert r.returncode == 0, r.stderr
    f = proj / ".beacon" / "triggers" / "review-due-ms-1.json"
    assert f.exists(), "trigger must survive — attainment is still owed"
    assert json.loads(f.read_text())["bindings"] == ["attainment"]


def test_sole_binding_target_skip_removes_file(proj):
    _seed_target_review_due(proj, "ms-2", ["attainment"])
    r = _run(proj, ["review", "skip", "--type", "attainment", "--target", "ms-2",
                    "--reason", "owner waived"], {"BEACON_SESSION_KIND": "human"})
    assert r.returncode == 0, r.stderr
    assert not (proj / ".beacon" / "triggers" / "review-due-ms-2.json").exists()
