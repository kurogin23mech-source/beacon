"""Tests for the review firing spine (ms-119 e-3911).

Two layers:
  - pure binding table (review_spine.review_bindings_for_transition)
  - the trigger-emit hook wired into the real transition commands
    (milestone done, gated done --review, operation close) + cleanup.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import review_spine  # noqa: E402
import commands  # noqa: E402


# --- pure binding table ---------------------------------------------------

def test_routine_transitions_bind_nothing():
    # start / pause / soft-monitoring carry no completion claim.
    assert review_spine.review_bindings_for_transition("milestone", "todo", "in_progress") == []
    assert review_spine.review_bindings_for_transition("milestone", "in_progress", "waiting") == []
    assert review_spine.review_bindings_for_transition("milestone", "in_progress", "observing") == []


def test_attainment_without_gate_or_spec_binds_attainment_only():
    b = review_spine.review_bindings_for_transition("milestone", "in_progress", "done")
    assert [x["review"] for x in b] == [review_spine.REVIEW_ATTAINMENT]
    assert b[0]["blocking"] is False  # advisory nudge, not a second block


def test_attainment_with_spec_binds_attainment_and_philosophy():
    b = review_spine.review_bindings_for_transition(
        "milestone", "in_progress", "done", has_spec=True)
    assert [x["review"] for x in b] == [
        review_spine.REVIEW_ATTAINMENT, review_spine.REVIEW_PHILOSOPHY]


def test_gated_binds_both_attainment_and_philosophy():
    # ms-119 e-4005: the 目的達成 evidence review auto-fires at the close 節目 on
    # the gated (--review) path too, alongside 思想 — both are wanted at close.
    b = review_spine.review_bindings_for_transition(
        "milestone", "in_progress", "done", has_spec=True, gated=True)
    assert [x["review"] for x in b] == [
        review_spine.REVIEW_ATTAINMENT, review_spine.REVIEW_PHILOSOPHY]
    assert b[0]["gated"] is True  # carried so the message can phrase it
    # Without a SPEC, a gated completion still fires the attainment evidence review.
    b2 = review_spine.review_bindings_for_transition(
        "milestone", "in_progress", "done", gated=True)
    assert [x["review"] for x in b2] == [review_spine.REVIEW_ATTAINMENT]


def test_operation_close_is_attainment():
    b = review_spine.review_bindings_for_transition("operation", "open", "closed")
    assert [x["review"] for x in b] == [review_spine.REVIEW_ATTAINMENT]


# --- firing hook (wired into the real commands) ---------------------------

@pytest.fixture
def proj(tmp_path, monkeypatch):
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "project.json").write_text(json.dumps({
        "name": "t",
        "milestones": [{"id": "ms-5", "title": "T", "status": "in_progress",
                        "entries": []}],
        "operations": [{"id": "op-3", "title": "O", "status": "open",
                        "entries": []}],
    }, ensure_ascii=False))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(commands, "_actor_str", lambda: "m/a")
    monkeypatch.setattr(commands, "_resolve_session_id", lambda: "sv-t")
    # No SPEC docs in the tmp project by default → has_spec=False.
    monkeypatch.setattr(commands, "_spec_exists_for_ms", lambda _id: False)
    monkeypatch.setattr(commands, "_spec_exists_for_op", lambda _id: False)
    return tmp_path


def _triggers(proj):
    d = proj / ".beacon" / "triggers"
    if not d.is_dir():
        return {}
    out = {}
    for f in d.glob("review-due-*.json"):
        out[f.name] = json.loads(f.read_text())
    return out


def _clear_env(monkeypatch):
    for k in ("BEACON_MS_ID", "BEACON_REASON", "BEACON_REVIEW",
              "BEACON_OPERATION_ID"):
        monkeypatch.delenv(k, raising=False)


def test_milestone_done_fires_review_due(proj, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_MS_ID", "ms-5")
    monkeypatch.setenv("BEACON_REASON", "完了")
    commands.cmd_milestone_done()
    trigs = _triggers(proj)
    assert "review-due-ms-5.json" in trigs
    t = trigs["review-due-ms-5.json"]
    assert t["kind"] == "review-due"
    assert t["bindings"] == [review_spine.REVIEW_ATTAINMENT]  # no SPEC → nudge only
    assert t["gated"] is False


def test_gated_done_fires_attainment_without_spec(proj, monkeypatch):
    # --review path, no SPEC: the 目的達成 evidence review still auto-fires at the
    # close 節目 (ms-119 e-4005). No 思想 原典 → philosophy absent.
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_MS_ID", "ms-5")
    monkeypatch.setenv("BEACON_REASON", "完了主張")
    monkeypatch.setenv("BEACON_REVIEW", "1")
    commands.cmd_milestone_done()
    t = _triggers(proj)["review-due-ms-5.json"]
    assert t["bindings"] == [review_spine.REVIEW_ATTAINMENT]
    assert t["gated"] is True


def test_gated_done_fires_attainment_and_philosophy_with_spec(proj, monkeypatch):
    monkeypatch.setattr(commands, "_spec_exists_for_ms", lambda _id: True)
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_MS_ID", "ms-5")
    monkeypatch.setenv("BEACON_REASON", "完了主張")
    monkeypatch.setenv("BEACON_REVIEW", "1")
    commands.cmd_milestone_done()
    t = _triggers(proj)["review-due-ms-5.json"]
    assert t["bindings"] == [
        review_spine.REVIEW_ATTAINMENT, review_spine.REVIEW_PHILOSOPHY]
    assert t["gated"] is True


def test_operation_close_fires_review_due(proj, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_OPERATION_ID", "op-3")
    commands.cmd_operation_close()
    t = _triggers(proj)["review-due-op-3.json"]
    assert t["target_kind"] == "operation"
    assert t["new_state"] == "closed"


# --- cleanup --------------------------------------------------------------

def test_cleanup_removes_when_target_reopened(proj, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_MS_ID", "ms-5")
    monkeypatch.setenv("BEACON_REASON", "完了")
    commands.cmd_milestone_done()
    assert "review-due-ms-5.json" in _triggers(proj)
    # Re-open the milestone → the completion was undone → nudge should clear.
    data = commands.load_project()
    next(m for m in data["milestones"] if m["id"] == "ms-5")["status"] = "in_progress"
    commands.save_project(data, op={"op": "test-reopen"})
    commands._cleanup_review_due_triggers()
    assert "review-due-ms-5.json" not in _triggers(proj)
