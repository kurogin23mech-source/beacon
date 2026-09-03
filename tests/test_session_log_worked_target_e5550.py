"""ms-153 e-5550 → ms-164 e-5942 (SPEC 方針3 / 問題 P3): session-end attributes
its summary to the Target(s) the session actually worked on — not a project-wide
log.

ms-164 makes attribution MULTI: a session that touched several Targets in a day
attributes to ALL of them (``target_ids``), reachable from the root AND from each
child Target — the older single-target path folded a cross-target session to
``ambiguous`` → unattributed. The resolution RULE itself now lives in the pure,
occupation-generic ``occupation.resolve_worked_targets`` (実装順序1, unit-tested in
``test_occupation_worked_targets_ms164.py``); these tests cover the session-log
INTEGRATION — that ``aggregate_session`` reads the fork hint + entry Targets and
stamps the multi set. No CLI, no cloud.
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


# --- _read_fork_target_id (the filesystem half) -----------------------------

def test_read_fork_target_id():
    with tempfile.TemporaryDirectory() as tmp:
        bd = Path(tmp)
        (bd / "fork.json").write_text(
            json.dumps({"target_ms_id": "ms-153", "child_branch": "x"}),
            encoding="utf-8")
        assert session_log._read_fork_target_id(bd) == "ms-153"


def test_read_fork_target_id_missing_or_malformed():
    with tempfile.TemporaryDirectory() as tmp:
        bd = Path(tmp)
        assert session_log._read_fork_target_id(bd) == ""  # no file
        (bd / "fork.json").write_text("{ not json", encoding="utf-8")
        assert session_log._read_fork_target_id(bd) == ""  # malformed
        (bd / "fork.json").write_text(json.dumps({"child_branch": "x"}),
                                      encoding="utf-8")
        assert session_log._read_fork_target_id(bd) == ""  # no target field


# --- aggregate_session carries the (multi) attribution ----------------------

def test_aggregate_attributes_to_fork_target():
    with tempfile.TemporaryDirectory() as tmp:
        bd = Path(tmp)
        (bd / "fork.json").write_text(
            json.dumps({"target_ms_id": "ms-153"}), encoding="utf-8")
        data = {"milestones": [
            {"id": "ms-153", "entries": [_commit("e-1", "S1", "work")]}]}
        payload = session_log.aggregate_session(
            project_data=data, beacon_dir=bd, session_id="S1")
        assert payload["target_ids"] == ["ms-153"]
        assert payload["target_id"] == "ms-153"  # back-compat first-of-set
        assert payload["target_source"] == "fork"


def test_aggregate_fork_unions_other_touched_targets():
    """ms-164: a fork session that ALSO committed to another Target attributes to
    both — the fork Target leads, the other is kept (not dropped as before)."""
    with tempfile.TemporaryDirectory() as tmp:
        bd = Path(tmp)
        (bd / "fork.json").write_text(
            json.dumps({"target_ms_id": "ms-153"}), encoding="utf-8")
        data = {"milestones": [
            {"id": "ms-153", "entries": [_commit("e-1", "S1", "work")]},
            {"id": "ms-99", "entries": [_commit("e-2", "S1", "side")]}]}
        payload = session_log.aggregate_session(
            project_data=data, beacon_dir=bd, session_id="S1")
        assert payload["target_ids"] == ["ms-153", "ms-99"]
        assert payload["target_source"] == "fork"


def test_aggregate_infers_target_without_fork():
    with tempfile.TemporaryDirectory() as tmp:
        bd = Path(tmp)
        data = {"milestones": [
            {"id": "ms-42", "entries": [_commit("e-1", "S1", "work")]}]}
        payload = session_log.aggregate_session(
            project_data=data, beacon_dir=bd, session_id="S1")
        assert payload["target_ids"] == ["ms-42"]
        assert payload["target_id"] == "ms-42"
        assert payload["target_source"] == "inferred"


def test_aggregate_multi_inferred_keeps_all_targets():
    """The behavioural heart of ms-164 e-5942: a non-fork session spanning two
    Targets now attributes to BOTH (was ``ambiguous`` → unattributed)."""
    with tempfile.TemporaryDirectory() as tmp:
        bd = Path(tmp)
        data = {"milestones": [
            {"id": "ms-1", "entries": [_commit("e-1", "S1", "a")]},
            {"id": "ms-2", "entries": [_commit("e-2", "S1", "b")]}]}
        payload = session_log.aggregate_session(
            project_data=data, beacon_dir=bd, session_id="S1")
        assert payload["target_ids"] == ["ms-1", "ms-2"]
        assert payload["target_source"] == "inferred"


def test_aggregate_project_wide_when_no_work():
    with tempfile.TemporaryDirectory() as tmp:
        bd = Path(tmp)
        data = {"milestones": []}
        payload = session_log.aggregate_session(
            project_data=data, beacon_dir=bd, session_id="S1")
        assert payload["target_ids"] == []
        assert payload["target_id"] == ""
        assert payload["target_source"] == "none"
