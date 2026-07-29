"""ms-128 方針4 (e-4365) chunk 5: `beacon trek block` / `beacon trek blockers` CLI.

Subprocess smoke tests over bin/beacon → commands.py → trek_store (local mode,
BEACON_TREKS_DIR isolated). Confirms the bash wiring collects multiple --on
blockers, that add_blocker's guards (self-block, cycle) surface as CLI errors,
and that `blockers` renders the blocked queue + cycles.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BEACON = REPO_ROOT / "bin" / "beacon"


def _run(env_extra: dict, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update(env_extra)
    cwd = env.pop("BEACON_CWD", None)
    return subprocess.run(
        [str(BEACON), "trek", *args],
        env=env, capture_output=True, text=True, cwd=cwd,
    )


@pytest.fixture
def trek_env(tmp_path):
    treks_dir = tmp_path / "treks"
    project_file = tmp_path / ".beacon" / "project.json"
    project_file.parent.mkdir(parents=True, exist_ok=True)
    project_file.write_text('{"name":"test","milestones":[],"operations":[]}\n')
    fake_beacon_home = tmp_path / "fake-beacon-home"
    fake_beacon_home.mkdir()
    return {
        "BEACON_TREKS_DIR": str(treks_dir),
        "BEACON_USER_ID": "u-test",
        "BEACON_USER_EMAIL": "test@example.com",
        "BEACON_SESSION_ID": "sv-test-1",
        "BEACON_CWD": str(tmp_path),
        "BEACON_HOME": str(fake_beacon_home),
        "BEACON_TREK_CONSENT_ACK": "1",
    }


@pytest.fixture
def trek_id(trek_env):
    r = _run(trek_env, "create", "Block Trek", "--json")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)["trek_id"]


def test_block_single_dependency(trek_env, trek_id):
    r = _run(trek_env, "block", trek_id, "ms-A", "--on", "ms-B", "--json")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert doc["task_states"]["ms-A"]["state"] == "block"
    assert doc["target_blockers"]["ms-A"] == ["ms-B"]


def test_block_multiple_dependencies(trek_env, trek_id):
    r = _run(trek_env, "block", trek_id, "ms-A",
             "--on", "ms-B", "--on", "ms-C", "--json")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert doc["target_blockers"]["ms-A"] == ["ms-B", "ms-C"]
    assert doc["task_states"]["ms-A"]["state"] == "block"


def test_block_requires_on(trek_env, trek_id):
    r = _run(trek_env, "block", trek_id, "ms-A", "--json")
    assert r.returncode != 0
    assert "--on" in r.stderr


def test_self_block_rejected_via_cli(trek_env, trek_id):
    r = _run(trek_env, "block", trek_id, "ms-A", "--on", "ms-A", "--json")
    assert r.returncode != 0
    assert "itself" in r.stderr.lower() or "block" in r.stderr.lower()


def test_cycle_rejected_via_cli(trek_env, trek_id):
    r1 = _run(trek_env, "block", trek_id, "ms-A", "--on", "ms-B", "--json")
    assert r1.returncode == 0, r1.stderr
    r2 = _run(trek_env, "block", trek_id, "ms-B", "--on", "ms-A", "--json")
    assert r2.returncode != 0
    assert "cycle" in r2.stderr.lower()


def test_blockers_view_lists_blocked_targets(trek_env, trek_id):
    _run(trek_env, "block", trek_id, "ms-A", "--on", "ms-B", "--on", "ms-C")
    r = _run(trek_env, "blockers", trek_id, "--json")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert len(out["blocked_queue"]) == 1
    row = out["blocked_queue"][0]
    assert row["task_id"] == "ms-A"
    assert set(row["unsatisfied"]) == {"ms-B", "ms-C"}
    assert out["blocker_cycles"] == []


def test_blockers_view_empty(trek_env, trek_id):
    r = _run(trek_env, "blockers", trek_id, "--json")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["blocked_queue"] == []


def test_blockers_view_human_readable(trek_env, trek_id):
    _run(trek_env, "block", trek_id, "ms-A", "--on", "ms-B")
    r = _run(trek_env, "blockers", trek_id)
    assert r.returncode == 0, r.stderr
    assert "ms-A" in r.stdout
    assert "ms-B" in r.stdout


# --- review hardening (2026-07-29) -----------------------------------------

def test_block_noop_reports_skipped_not_blocked(trek_env, trek_id):
    # Satisfy ms-B first, then block ms-A on it → should report skipped, not blocked.
    _run(trek_env, "task-state", trek_id, "ms-B", "working")
    _run(trek_env, "task-state", trek_id, "ms-B", "leader_review")
    r = _run(trek_env, "block", trek_id, "ms-A", "--on", "ms-B")
    assert r.returncode == 0, r.stderr
    assert "skipped ms-B" in r.stdout
    assert "何も張られていません" in r.stdout


def test_block_mixed_reports_each_edge(trek_env, trek_id):
    _run(trek_env, "task-state", trek_id, "ms-B", "working")
    _run(trek_env, "task-state", trek_id, "ms-B", "leader_review")  # satisfied
    r = _run(trek_env, "block", trek_id, "ms-A", "--on", "ms-B", "--on", "ms-C")
    assert r.returncode == 0, r.stderr
    assert "skipped ms-B" in r.stdout
    assert "blocked on ms-C" in r.stdout


def test_task_state_rejects_block(trek_env, trek_id):
    r = _run(trek_env, "task-state", trek_id, "ms-A", "block")
    assert r.returncode != 0
    assert "beacon trek block" in r.stderr


def test_unblock_removes_edge_and_reconciles(trek_env, trek_id):
    _run(trek_env, "block", trek_id, "ms-A", "--on", "ms-B")
    r = _run(trek_env, "unblock", trek_id, "ms-A", "--on", "ms-B", "--json")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert "ms-A" not in (doc.get("target_blockers") or {})
    # edge-less block reconciles back to todo
    assert doc["task_states"]["ms-A"]["state"] == "todo"


def test_unblock_one_edge_of_two_keeps_block(trek_env, trek_id):
    _run(trek_env, "block", trek_id, "ms-A", "--on", "ms-B", "--on", "ms-C")
    r = _run(trek_env, "unblock", trek_id, "ms-A", "--on", "ms-B", "--json")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert doc["target_blockers"]["ms-A"] == ["ms-C"]
    assert doc["task_states"]["ms-A"]["state"] == "block"  # still waiting on ms-C
