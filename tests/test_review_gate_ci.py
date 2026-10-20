"""CI-side review gate scaffold (ms-119 / e-4073).

The scaffold makes the review gate path-independent via a `beacon-review-gate`
commit status. These tests pin the PURE logic (which reviews the gate requires,
the status payload shape) and that the local flip is OFF by default so nothing
posts to GitHub or spends anything until a maintainer opts in.
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "review-gate-ci.py"
sys.path.insert(0, str(ROOT / "lib"))
import commands  # noqa: E402


def _load_script_module():
    spec = importlib.util.spec_from_file_location("review_gate_ci", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = _load_script_module()


# --- pure logic -------------------------------------------------------------

def test_required_review_types_is_the_pr_open_pair():
    got = set(gate.required_review_types())
    assert "ax" in got and "maintainability" in got


def test_status_payload_pending_shape():
    p = gate.status_payload("pending", pr="42")
    assert p["context"] == "beacon-review-gate"
    assert p["state"] == "pending"
    assert p["pr"] == "42"
    assert "ax" in p["required_reviews"]
    assert len(p["description"]) <= 140


def test_status_payload_success_description():
    p = gate.status_payload("success")
    assert p["state"] == "success"
    assert "記録済み" in p["description"]


def test_plan_subcommand_prints_payload():
    r = subprocess.run([sys.executable, str(SCRIPT), "plan", "--pr", "7"],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert out["state"] == "pending" and out["pr"] == "7"


# --- the local flip follows the REPOSITORY, not the operator's shell -------
#
# ms-181 e-7103 changed the question this asks. It used to pin "no env var →
# touch nothing at all", which protected repositories that do not use the gate
# but ALSO meant an operator who ran `beacon review done` without remembering
# `BEACON_REVIEW_GATE_CI=1` left the check pending — measured twice (ms-185's
# fork 2026-10-19, PR #811 2026-10-20) and the second half of e-7103's
# two-sided hole. The protected property is kept: a repository that has not
# opted in is still left alone. What changed is who is asked — the repo
# variable the workflow itself is gated on, instead of a shell the operator has
# to remember.

def test_ci_flip_leaves_a_repo_that_has_not_opted_in_alone(monkeypatch):
    """No env var + repo variable unset → no status is pushed.

    This is what the old "no-op by default" test was really protecting, and it
    must keep holding: deriving the opt-in must not start stamping commit
    statuses onto repositories that never asked for the gate.
    """
    monkeypatch.delenv("BEACON_REVIEW_GATE_CI", raising=False)
    calls = []

    class _Unset:
        returncode = 1
        stdout = ""

    def rec(argv, *a, **k):
        calls.append(argv)
        return _Unset()

    monkeypatch.setattr(commands.subprocess, "run", rec)
    commands._ci_flip_review_gate_success("42")
    # It may ASK (one `gh variable get`), but it must not go on to resolve a
    # head sha or invoke the gate script.
    assert all("headRefOid" not in " ".join(c) for c in calls), calls
    assert all("review-gate-ci.py" not in " ".join(c) for c in calls), calls


def test_ci_flip_needs_no_env_var_when_the_repo_uses_the_gate(monkeypatch):
    """受入条件 5: the stamp lands without anyone remembering a variable."""
    monkeypatch.delenv("BEACON_REVIEW_GATE_CI", raising=False)
    calls = []

    class R:
        returncode = 0

        def __init__(self, argv):
            # `gh variable get` answers "1"; the sha probes answer a sha.
            self.stdout = "1\n" if "variable" in argv else "abc123sha\n"

    def rec(argv, *a, **k):
        calls.append(argv)
        return R(argv)

    monkeypatch.setattr(commands.subprocess, "run", rec)
    commands._ci_flip_review_gate_success("42")
    assert any("variable" in " ".join(c) for c in calls), calls
    assert any("headRefOid" in " ".join(c) for c in calls), calls


def test_ci_flip_can_still_be_turned_off_explicitly(monkeypatch):
    """`BEACON_REVIEW_GATE_CI=0` asks nothing and pushes nothing.

    Keeps an escape hatch for a repository that has the variable set but a
    context where stamping is wrong (a probe, a replay, someone else's PR).
    """
    monkeypatch.setenv("BEACON_REVIEW_GATE_CI", "0")

    def boom(*a, **k):
        raise AssertionError("an explicit 0 must not consult gh at all")

    monkeypatch.setattr(commands.subprocess, "run", boom)
    commands._ci_flip_review_gate_success("42")


def test_ci_flip_attempts_gh_when_enabled(monkeypatch):
    monkeypatch.setenv("BEACON_REVIEW_GATE_CI", "1")
    calls = []

    class R:
        stdout = "abc123sha\n"
        returncode = 0

    def rec(argv, *a, **k):
        calls.append(argv)
        return R()

    monkeypatch.setattr(commands.subprocess, "run", rec)
    commands._ci_flip_review_gate_success("42")
    # first call resolves the head sha, second invokes the gate script set.
    assert any("headRefOid" in " ".join(c) for c in calls)
    assert any("review-gate-ci.py" in " ".join(c) and "success" in c for c in calls)


# --- activation plan builders (ms-160 e-5805) ------------------------------
# Pure functions: they turn a gate_status() snapshot into the exact gh argv,
# so we can pin the two repo-admin steps without mutating a live repo.

def _status(*, var=None, protected=False, checks=None):
    checks = checks or []
    return {
        "branch": "main",
        "variable_BEACON_REVIEW_GATE_CI": var,
        "branch_protected": protected,
        "required_checks": checks,
        "gate_required": gate.GATE_CONTEXT in checks,
        "active": var == "1" and gate.GATE_CONTEXT in checks,
    }


def test_activate_plan_from_scratch_sets_var_and_creates_protection():
    """Unprotected main + unset variable → both steps, and the protection PUT
    requires exactly the gate check (minimal policy by default)."""
    plan = gate.build_activate_plan(_status(var=None, protected=False))
    labels = " | ".join(s["label"] for s in plan)
    assert "BEACON_REVIEW_GATE_CI=1" in labels
    assert "create branch protection" in labels
    put = [s for s in plan if "--method" in s["argv"] and "PUT" in s["argv"]][0]
    body = json.loads(put["stdin"])
    assert body["required_status_checks"]["contexts"] == [gate.GATE_CONTEXT]
    # Minimal by default: no admin enforcement, no PR-review requirement.
    assert body["enforce_admins"] is False
    assert body["required_pull_request_reviews"] is None


def test_activate_plan_is_additive_when_already_protected():
    """Existing protection with an unrelated check → PATCH that KEEPS the other
    check and adds ours (never clobbers existing required checks)."""
    plan = gate.build_activate_plan(
        _status(var="1", protected=True, checks=["test"]))
    # variable already 1 → only the context-add step remains.
    assert len(plan) == 1
    patch = plan[0]
    assert "PATCH" in patch["argv"]
    body = json.loads(patch["stdin"])
    assert set(body["contexts"]) == {"test", gate.GATE_CONTEXT}


def test_activate_plan_empty_when_already_active():
    plan = gate.build_activate_plan(
        _status(var="1", protected=True, checks=[gate.GATE_CONTEXT]))
    assert plan == []


def test_activate_plan_honors_stricter_policy_flags():
    plan = gate.build_activate_plan(
        _status(var="1", protected=False),
        enforce_admins=True, require_pr_reviews=2)
    put = [s for s in plan if "PUT" in s["argv"]][0]
    body = json.loads(put["stdin"])
    assert body["enforce_admins"] is True
    assert body["required_pull_request_reviews"][
        "required_approving_review_count"] == 2


def test_deactivate_plan_sets_var_zero_and_drops_context_only():
    plan = gate.build_deactivate_plan(
        _status(var="1", protected=True, checks=["test", gate.GATE_CONTEXT]))
    labels = " | ".join(s["label"] for s in plan)
    assert "BEACON_REVIEW_GATE_CI=0" in labels
    patch = [s for s in plan if "PATCH" in s["argv"]][0]
    body = json.loads(patch["stdin"])
    # the unrelated check survives; only the gate context is dropped.
    assert body["contexts"] == ["test"]


def test_execute_plan_dry_run_never_shells_out(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("dry-run must not execute gh")
    monkeypatch.setattr(gate.subprocess, "run", boom)
    plan = gate.build_activate_plan(_status(var=None, protected=False))
    results = gate._execute_plan(plan, dry_run=True)
    assert results and all(r["dry_run"] for r in results)


def test_execute_plan_stops_at_first_failure(monkeypatch):
    calls = []

    class R:
        def __init__(self, rc):
            self.returncode = rc
            self.stdout = ""
            self.stderr = "boom" if rc else ""

    def rec(argv, *a, **k):
        calls.append(argv)
        return R(1)  # first step fails

    monkeypatch.setattr(gate.subprocess, "run", rec)
    plan = gate.build_activate_plan(_status(var=None, protected=False))
    assert len(plan) == 2
    results = gate._execute_plan(plan, dry_run=False)
    # stopped after the first (failing) step — the second never ran.
    assert len(results) == 1 and results[0]["ok"] is False
    assert len(calls) == 1
