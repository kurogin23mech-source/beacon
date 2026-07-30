"""beacon-log tolerates milestone-less (non-dev) projects (ms-133 e-4650).

The post-commit hook fires /beacon-log on EVERY commit, and its prepare/finalize
CLIs used to hard-error "No active milestone". In a sales / backoffice project
(which drives work through opportunities, not milestones, so milestones[] is
empty by construction) that failed the hook on any commit. These pin that the
three log entry points degrade gracefully for a non-dev project while dev is
unchanged.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
COMMANDS = ROOT / "lib" / "commands.py"


def _run(subcmd, project_file, extra_env=None):
    env = dict(os.environ)
    env["BEACON_PROJECT_FILE"] = str(project_file)
    env["BEACON_HASH"] = "abc1234"
    env["BEACON_MESSAGE"] = "test commit"
    env["BEACON_BEHAVIOR"] = "b"
    env.pop("BEACON_MS_ID", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(COMMANDS), subcmd],
        capture_output=True, text=True, env=env,
    )


@pytest.fixture
def sales_project(tmp_path):
    pf = tmp_path / ".beacon" / "project.json"
    pf.parent.mkdir(parents=True)
    pf.write_text(json.dumps({
        "name": "S", "objective": "o", "profession": "sales",
        "milestones": [], "opportunities": [], "accounts": [],
    }), encoding="utf-8")
    return pf


@pytest.fixture
def dev_project(tmp_path):
    pf = tmp_path / ".beacon" / "project.json"
    pf.parent.mkdir(parents=True)
    pf.write_text(json.dumps({
        "name": "D", "objective": "o", "profession": "dev",
        "milestones": [{"id": "ms-1", "title": "M", "status": "in_progress",
                        "entries": [], "progress": 0}],
    }), encoding="utf-8")
    return pf


def test_prepare_is_graceful_on_sales(sales_project):
    r = _run("log_prepare", sales_project)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["milestone_binding"] == "none"
    assert out["profession"] == "sales"
    assert "milestone" not in out and "candidates" not in out


def test_finalize_is_graceful_on_sales(sales_project):
    r = _run("log_finalize", sales_project)
    assert r.returncode == 0, r.stderr
    assert "no milestone binding" in r.stdout.lower() or "not milestone" in r.stdout.lower()


def test_bare_log_is_graceful_on_sales(sales_project):
    r = _run("log", sales_project, {"BEACON_SUMMARY": "s"})
    assert r.returncode == 0, r.stderr
    assert "not milestone-bound" in r.stdout.lower()


def test_dev_prepare_still_binds_milestone(dev_project):
    """dev is unchanged: a single active milestone is resolved normally."""
    r = _run("log_prepare", dev_project)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out.get("milestone", {}).get("id") == "ms-1"
    assert out.get("milestone_binding") != "none"


def test_dev_with_no_active_milestone_still_errors(tmp_path):
    """A DEV project with no active milestone must STILL error (unchanged) —
    the graceful path is only for occupations that have no milestones by design,
    not a dev project that simply hasn't started one."""
    pf = tmp_path / ".beacon" / "project.json"
    pf.parent.mkdir(parents=True)
    pf.write_text(json.dumps({
        "name": "D", "objective": "o", "profession": "dev", "milestones": [],
    }), encoding="utf-8")
    r = _run("log_prepare", pf)
    assert r.returncode != 0
    assert "No active milestone" in (r.stdout + r.stderr)
