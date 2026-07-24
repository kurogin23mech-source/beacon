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


# --- local flip is OFF by default ------------------------------------------

def test_ci_flip_is_noop_by_default(monkeypatch):
    """Without BEACON_REVIEW_GATE_CI=1 the local flip must not shell out to gh
    (default OFF — the CI gate is opt-in scaffolding, spends nothing)."""
    monkeypatch.delenv("BEACON_REVIEW_GATE_CI", raising=False)
    called = {"n": 0}

    def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("subprocess should not run when gate CI is OFF")

    monkeypatch.setattr(commands.subprocess, "run", boom)
    commands._ci_flip_review_gate_success("42")  # must be a no-op
    assert called["n"] == 0


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
