"""ms-153 e-5550 (SPEC 方針3 / 問題 P3): session-end writes its summary scoped to
the TARGET the session actually worked on — not a project-wide log.

- In a fork worktree the write target is STRUCTURALLY fixed by fork.json's
  ``target_ms_id`` (受入条件6).
- Otherwise it is inferred from the session's own entries: one Target → that
  Target; none / several → left empty (stays unattributed rather than guessing).

These are pure-function tests over ``resolve_worked_target`` /
``collect_project_entries`` / ``aggregate_session`` — no CLI, no cloud.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import session_log  # noqa: E402


def _commit(eid, sid, msg=""):
    return {"id": eid, "type": "commit",
            "meta": {"session_id": sid, "message": msg}}


# --- collect_project_entries surfaces the touched Targets --------------------

def test_collect_surfaces_target_ids():
    data = {"milestones": [
        {"id": "ms-1", "entries": [_commit("e-1", "S1"), _commit("e-2", "S1")]},
        {"id": "ms-2", "entries": [_commit("e-3", "S1")]},
        {"id": "ms-3", "entries": [_commit("e-9", "OTHER")]},  # other session
    ]}
    out = session_log.collect_project_entries(data, "S1")
    assert out["commit_ids"] == ["e-1", "e-2", "e-3"]
    # ms-1 (first-seen) then ms-2; ms-3 excluded (its commit is another session)
    assert out["target_ids"] == ["ms-1", "ms-2"]


# --- resolve_worked_target --------------------------------------------------

def test_fork_fixes_target_structurally():
    with tempfile.TemporaryDirectory() as tmp:
        bd = Path(tmp)
        (bd / "fork.json").write_text(
            json.dumps({"target_ms_id": "ms-153", "child_branch": "x"}),
            encoding="utf-8")
        # even if the session touched OTHER targets, fork.json wins
        got = session_log.resolve_worked_target(bd, ["ms-99"])
        assert got == {"target_id": "ms-153", "target_source": "fork"}


def test_fork_json_malformed_falls_through_to_inference():
    with tempfile.TemporaryDirectory() as tmp:
        bd = Path(tmp)
        (bd / "fork.json").write_text("{ not json", encoding="utf-8")
        got = session_log.resolve_worked_target(bd, ["ms-7"])
        assert got == {"target_id": "ms-7", "target_source": "inferred"}


def test_fork_json_without_target_falls_through():
    with tempfile.TemporaryDirectory() as tmp:
        bd = Path(tmp)
        (bd / "fork.json").write_text(json.dumps({"child_branch": "x"}),
                                      encoding="utf-8")
        got = session_log.resolve_worked_target(bd, ["ms-7"])
        assert got["target_source"] == "inferred"
        assert got["target_id"] == "ms-7"


def test_inferred_single_target():
    with tempfile.TemporaryDirectory() as tmp:
        got = session_log.resolve_worked_target(Path(tmp), ["ms-5", "ms-5"])
        assert got == {"target_id": "ms-5", "target_source": "inferred"}


def test_none_when_no_entries():
    with tempfile.TemporaryDirectory() as tmp:
        got = session_log.resolve_worked_target(Path(tmp), [])
        assert got == {"target_id": "", "target_source": "none"}


def test_ambiguous_when_multiple_targets():
    with tempfile.TemporaryDirectory() as tmp:
        got = session_log.resolve_worked_target(Path(tmp), ["ms-1", "ms-2"])
        assert got == {"target_id": "", "target_source": "ambiguous"}


# --- aggregate_session carries the attribution ------------------------------

def test_aggregate_attributes_to_fork_target():
    with tempfile.TemporaryDirectory() as tmp:
        bd = Path(tmp)
        (bd / "fork.json").write_text(
            json.dumps({"target_ms_id": "ms-153"}), encoding="utf-8")
        data = {"milestones": [
            {"id": "ms-153", "entries": [_commit("e-1", "S1", "work")]}]}
        payload = session_log.aggregate_session(
            project_data=data, beacon_dir=bd, session_id="S1")
        assert payload["target_id"] == "ms-153"
        assert payload["target_source"] == "fork"


def test_aggregate_infers_target_without_fork():
    with tempfile.TemporaryDirectory() as tmp:
        bd = Path(tmp)
        data = {"milestones": [
            {"id": "ms-42", "entries": [_commit("e-1", "S1", "work")]}]}
        payload = session_log.aggregate_session(
            project_data=data, beacon_dir=bd, session_id="S1")
        assert payload["target_id"] == "ms-42"
        assert payload["target_source"] == "inferred"


def test_aggregate_project_wide_when_no_work():
    with tempfile.TemporaryDirectory() as tmp:
        bd = Path(tmp)
        data = {"milestones": []}
        payload = session_log.aggregate_session(
            project_data=data, beacon_dir=bd, session_id="S1")
        assert payload["target_id"] == ""
        assert payload["target_source"] == "none"
