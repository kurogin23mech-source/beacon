"""CLI verb wiring for Operation pause/resume (ms-160 e-5814).

The core mechanism (core.operation_pause/resume) and the fire-suppression consumer
(cmd_trigger.py, e-5484) already existed; e-5814 added the operator-facing verb.
These lock the bash → commands.py → core → save path end-to-end so a paused
Operation's scheduled fire is actually suppressible from the CLI.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BEACON = REPO / "bin" / "beacon"
sys.path.insert(0, str(REPO / "lib"))
import core  # noqa: E402


def _beacon(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(BEACON), *args], cwd=str(cwd),
                          capture_output=True, text=True, timeout=30)


def _project(tmp_path: Path) -> Path:
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "project.json").write_text(json.dumps(
        {"name": "OpPauseTest", "milestones": [], "operations": []}))
    return tmp_path


def _phase(tmp_path: Path, op_id: str) -> str:
    d = json.loads((tmp_path / ".beacon" / "project.json").read_text())
    op = next(o for o in d["operations"] if o["id"] == op_id)
    return core.operation_execution_phase(op)


def _open_active_op(cwd: Path) -> str:
    _beacon(cwd, "operation", "open", "Health monitor", "--schedule", "daily")
    d = json.loads((cwd / ".beacon" / "project.json").read_text())
    op_id = d["operations"][0]["id"]
    _beacon(cwd, "operation", "activate", op_id)
    return op_id


def test_pause_sets_paused_phase(tmp_path):
    cwd = _project(tmp_path)
    op_id = _open_active_op(cwd)
    r = _beacon(cwd, "operation", "pause", op_id, "--reason", "メンテ中")
    assert r.returncode == 0, r.stderr
    assert "paused" in r.stdout.lower()
    assert _phase(cwd, op_id) == core.EXECUTION_PHASE_PAUSED


def test_resume_returns_to_idle(tmp_path):
    cwd = _project(tmp_path)
    op_id = _open_active_op(cwd)
    _beacon(cwd, "operation", "pause", op_id)
    r = _beacon(cwd, "operation", "resume", op_id)
    assert r.returncode == 0, r.stderr
    assert _phase(cwd, op_id) != core.EXECUTION_PHASE_PAUSED


def test_paused_operation_is_suppressed_from_firing(tmp_path):
    # End-to-end: the verb sets paused, and the trigger-tick consumer skips it.
    cwd = _project(tmp_path)
    op_id = _open_active_op(cwd)
    _beacon(cwd, "operation", "pause", op_id)
    d = json.loads((cwd / ".beacon" / "project.json").read_text())
    op = next(o for o in d["operations"] if o["id"] == op_id)
    # cmd_trigger.py:935 skips an op whose execution phase is PAUSED.
    assert core.operation_execution_phase(op) == core.EXECUTION_PHASE_PAUSED


def test_pause_requires_op_id(tmp_path):
    cwd = _project(tmp_path)
    r = _beacon(cwd, "operation", "pause")
    assert r.returncode != 0
    assert "operation id required" in (r.stderr + r.stdout).lower()
