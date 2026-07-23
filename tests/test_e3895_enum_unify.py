"""ms-120 / e-3895: enum / flag name unification (remaining axes).

The --ms/-m and --ac/--acceptance-criteria alias axes shipped earlier. This
covers the value-level axes decided in this session:

  * priority: `medium` is canonical (matches every priority system's mid-tier),
    `middle` a deprecated alias. Both accepted; input normalizes to medium.
  * "show hidden too" flags: `--all` is accepted universally (doc list /
    incident list), alongside the self-describing --include-<state> names.

(task `in_progress` vs trek `working` was intentionally left un-unified: `working`
is trek-local and trek spans profession instances, so aligning it to the
dev-specific `in_progress` would be a false alignment — see the commit / memo.)
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin" / "beacon"
BASH = shutil.which("bash")
sys.path.insert(0, str(ROOT / "lib"))
import core  # noqa: E402


# --- priority: medium canonical, middle alias ------------------------------

def test_valid_priorities_are_medium_canonical():
    assert "medium" in core.VALID_PRIORITIES
    assert "middle" not in core.VALID_PRIORITIES
    # both accepted on input
    assert "middle" in core._ACCEPTED_PRIORITIES
    assert "medium" in core._ACCEPTED_PRIORITIES


def test_normalize_priority_maps_middle_to_medium():
    assert core.normalize_priority("middle") == "medium"
    assert core.normalize_priority("medium") == "medium"
    assert core.normalize_priority("high") == "high"


def test_task_add_stores_middle_as_medium():
    data = {"name": "t", "milestones": [
        {"id": "ms-1", "title": "m", "status": "todo", "entries": []}]}
    core.task_add(data, "ms-1", "T1", priority="middle")
    core.task_add(data, "ms-1", "T2", priority="medium")
    tasks = [e for e in data["milestones"][0]["entries"] if e.get("type") == "task"]
    assert tasks[0]["meta"]["priority"] == "medium"  # middle normalized
    assert tasks[1]["meta"]["priority"] == "medium"


def test_task_add_rejects_unknown_priority():
    data = {"name": "t", "milestones": [
        {"id": "ms-1", "title": "m", "status": "todo", "entries": []}]}
    with pytest.raises(ValueError) as exc:
        core.task_add(data, "ms-1", "bad", priority="mid")
    # error lists the canonical set (medium, not middle)
    assert "medium" in str(exc.value)
    assert "middle" not in str(exc.value)


# --- --all universal "show hidden" flag (bash) -----------------------------

pytestmark_bash = pytest.mark.skipif(BASH is None, reason="bash not available")


@pytest.fixture
def proj(tmp_path):
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "project.json").write_text(
        json.dumps({"name": "t", "milestones": [], "operations": []}),
        encoding="utf-8")
    return tmp_path


def _run(proj, *args):
    return subprocess.run([BASH, str(BIN), *args], cwd=proj,
                          capture_output=True, text=True)


@pytestmark_bash
def test_doc_list_accepts_all_and_include_trashed(proj):
    for flag in ("--all", "--include-trashed"):
        r = _run(proj, "doc", "list", flag)
        assert r.returncode == 0, (flag, r.stderr)
        assert "not a valid flag" not in r.stderr


@pytestmark_bash
def test_incident_list_accepts_all_and_include_closed(proj):
    for flag in ("--all", "--include-closed"):
        r = _run(proj, "incident", "list", flag)
        assert r.returncode == 0, (flag, r.stderr)
        assert "not a valid flag" not in r.stderr
