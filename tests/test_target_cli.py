"""CLI tests for `beacon target` — 目的達成レビュー surface (ms-119 e-3912).

Covers the CLI increment on top of the pure primitive + core wiring:
  - review-request creates a pending approval (and rejects routine transitions)
  - approve records the verdict AND executes the transition on the target
  - reject records the verdict and leaves the target unchanged
  - list surfaces pending requests
  - `milestone done --review` opt-in routes a completion through the gate
    instead of applying it directly (default path unchanged).
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import commands  # noqa: E402
import cmd_milestone  # noqa: E402  (ms-127 e-4849: cmd_milestone_done moved here)
import cmd_target  # noqa: E402  (ms-127 e-4852: cmd_target_* moved here)
import transition_approval as _ta  # noqa: E402


def _write_project(tmp_path, milestones, operations=None):
    (tmp_path / ".beacon").mkdir(exist_ok=True)
    (tmp_path / ".beacon" / "project.json").write_text(json.dumps({
        "name": "t",
        "milestones": milestones,
        "operations": operations or [],
    }, ensure_ascii=False))


@pytest.fixture
def proj(tmp_path, monkeypatch):
    _write_project(
        tmp_path,
        milestones=[{"id": "ms-5", "title": "T", "status": "in_progress",
                     "entries": []}],
        operations=[{"id": "op-3", "title": "O", "status": "open",
                     "entries": []}],
    )
    monkeypatch.chdir(tmp_path)
    # Keep the CLI hermetic: no reason gate prompts, deterministic actor.
    # ms-127 e-4849/e-4852: cmd_milestone_done moved to cmd_milestone and the
    # cmd_target_* handlers moved to cmd_target; each resolves these helpers in
    # its own namespace (imported from commands_shared), so patch there too or the
    # deterministic-actor claim silently breaks (the e-4320 rule).
    monkeypatch.setattr(commands, "_actor_str", lambda: "m/a")
    monkeypatch.setattr(commands, "_resolve_session_id", lambda: "sv-t")
    monkeypatch.setattr(cmd_milestone, "_actor_str", lambda: "m/a")
    monkeypatch.setattr(cmd_milestone, "_resolve_session_id", lambda: "sv-t")
    monkeypatch.setattr(cmd_target, "_actor_str", lambda: "m/a")
    monkeypatch.setattr(cmd_target, "_resolve_session_id", lambda: "sv-t")
    return tmp_path


def _clear_env(monkeypatch):
    for k in ("BEACON_TARGET_ID", "BEACON_NEW_STATE", "BEACON_OLD_STATE",
              "BEACON_INTENT", "BEACON_EVIDENCE", "BEACON_ENTRY_ID",
              "BEACON_RATIONALE", "BEACON_PENDING", "BEACON_JSON",
              "BEACON_MS_ID", "BEACON_REASON", "BEACON_REVIEW"):
        monkeypatch.delenv(k, raising=False)


def _reload(proj):
    return json.loads((proj / ".beacon" / "project.json").read_text())


def _entries(data, tid):
    coll = data["milestones"] + data["operations"]
    t = next(c for c in coll if c["id"] == tid)
    return [e for e in t.get("entries", [])
            if e.get("type") == "target-transition-approval"]


# --- review-request -------------------------------------------------------

def test_review_request_creates_pending(proj, monkeypatch, capsys):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_TARGET_ID", "ms-5")
    monkeypatch.setenv("BEACON_NEW_STATE", "done")
    monkeypatch.setenv("BEACON_INTENT", "AC 3/3, 本番2週 incident ゼロ")
    monkeypatch.setenv("BEACON_EVIDENCE", "e-1, e-2")
    commands.cmd_target_review_request()
    data = _reload(proj)
    entries = _entries(data, "ms-5")
    assert len(entries) == 1
    m = entries[0]["meta"]
    assert m["approval_status"] == "pending"
    assert m["old_state"] == "in_progress" and m["new_state"] == "done"
    assert m["evidence"] == ["e-1", "e-2"]
    assert "目的達成レビュー依頼" in capsys.readouterr().out


def test_review_request_rejects_routine_transition(proj, monkeypatch):
    # todo -> in_progress is a *start*, not an attainment claim → refused.
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_TARGET_ID", "ms-5")
    monkeypatch.setenv("BEACON_OLD_STATE", "todo")
    monkeypatch.setenv("BEACON_NEW_STATE", "in_progress")
    with pytest.raises(SystemExit):
        commands.cmd_target_review_request()
    # Nothing persisted.
    assert _entries(_reload(proj), "ms-5") == []


# --- approve → executes ---------------------------------------------------

def test_approve_executes_milestone_completion(proj, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_TARGET_ID", "ms-5")
    monkeypatch.setenv("BEACON_NEW_STATE", "done")
    commands.cmd_target_review_request()
    eid = _entries(_reload(proj), "ms-5")[0]["id"]

    # ms-119 / e-4205: approve now needs independent evidence attached first
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_ENTRY_ID", eid)
    monkeypatch.setenv("BEACON_EV_VERDICT", "attained")
    monkeypatch.setenv("BEACON_EV_SUMMARY", "全 AC met (独立 judge)")
    commands.cmd_target_attach_evidence()

    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_ENTRY_ID", eid)
    monkeypatch.setenv("BEACON_RATIONALE", "owner 承認")
    commands.cmd_target_approve()

    data = _reload(proj)
    ms = next(m for m in data["milestones"] if m["id"] == "ms-5")
    assert ms["status"] == "done"  # the transition actually executed
    ent = _entries(data, "ms-5")[0]
    assert ent["meta"]["approval_status"] == "approved"


def test_approve_executes_operation_close(proj, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_TARGET_ID", "op-3")
    monkeypatch.setenv("BEACON_NEW_STATE", "closed")
    commands.cmd_target_review_request()
    eid = _entries(_reload(proj), "op-3")[0]["id"]

    # ms-119 / e-4205: approve now needs independent evidence attached first
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_ENTRY_ID", eid)
    monkeypatch.setenv("BEACON_EV_VERDICT", "attained")
    monkeypatch.setenv("BEACON_EV_SUMMARY", "運用終了条件を満たす (独立 judge)")
    commands.cmd_target_attach_evidence()

    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_ENTRY_ID", eid)
    commands.cmd_target_approve()

    data = _reload(proj)
    op = next(o for o in data["operations"] if o["id"] == "op-3")
    assert op["status"] == "closed"


# --- reject → no transition ----------------------------------------------

def test_reject_leaves_target_unchanged(proj, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_TARGET_ID", "ms-5")
    monkeypatch.setenv("BEACON_NEW_STATE", "done")
    commands.cmd_target_review_request()
    eid = _entries(_reload(proj), "ms-5")[0]["id"]

    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_ENTRY_ID", eid)
    monkeypatch.setenv("BEACON_RATIONALE", "まだ AC 未達")
    commands.cmd_target_reject()

    data = _reload(proj)
    ms = next(m for m in data["milestones"] if m["id"] == "ms-5")
    assert ms["status"] == "in_progress"  # NOT transitioned
    assert _entries(data, "ms-5")[0]["meta"]["approval_status"] == "rejected"


# --- list -----------------------------------------------------------------

def test_list_json_surfaces_pending(proj, monkeypatch, capsys):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_TARGET_ID", "ms-5")
    monkeypatch.setenv("BEACON_NEW_STATE", "done")
    commands.cmd_target_review_request()
    capsys.readouterr()

    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_PENDING", "1")
    monkeypatch.setenv("BEACON_JSON", "1")
    commands.cmd_target_list()
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1
    assert rows[0]["meta"]["target_id"] == "ms-5"


# --- opt-in routing on `milestone done --review` --------------------------

def test_milestone_done_review_routes_to_gate(proj, monkeypatch, capsys):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_MS_ID", "ms-5")
    monkeypatch.setenv("BEACON_REASON", "完了主張")
    monkeypatch.setenv("BEACON_REVIEW", "1")
    commands.cmd_milestone_done()

    data = _reload(proj)
    ms = next(m for m in data["milestones"] if m["id"] == "ms-5")
    # Completion was NOT applied — it is pending human approval instead.
    assert ms["status"] == "in_progress"
    entries = _entries(data, "ms-5")
    assert len(entries) == 1
    assert entries[0]["meta"]["new_state"] == "done"
    assert entries[0]["meta"]["intent"] == "完了主張"


# --- weak-AC gap surfacing (e-3911 §5 AC6) ---------------------------------

def test_assess_criteria_strong_when_spec_present():
    # A SPEC 原典 is enough written criteria → no gap (intent still required).
    assert _ta.assess_completion_criteria(has_spec=True, intent="done") == []


def test_assess_criteria_flags_no_written_criteria_and_no_intent():
    gaps = _ta.assess_completion_criteria(
        has_spec=False, objective="", acceptance="", intent="")
    assert "no_written_criteria" in gaps
    assert "no_intent" in gaps


def test_assess_criteria_objective_counts_as_written():
    gaps = _ta.assess_completion_criteria(
        has_spec=False, objective="本番2週 incident ゼロ", intent="ok")
    assert gaps == []  # objective は書かれた基準としてカウント


def test_format_criteria_gap_empty_when_no_reasons():
    assert _ta.format_criteria_gap([], target_id="ms-5") == ""


def test_review_request_surfaces_gap_for_weak_target(proj, monkeypatch, capsys):
    # ms-5 in the fixture has no SPEC (get_store empty), no objective/AC.
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_TARGET_ID", "ms-5")
    monkeypatch.setenv("BEACON_NEW_STATE", "done")
    # no intent → both gaps; request is still created (non-blocking).
    commands.cmd_target_review_request()
    out = capsys.readouterr().out
    assert "完了条件の gap" in out
    assert "hard-block はしません" in out
    # The pending request WAS created despite the gap.
    assert len(_entries(_reload(proj), "ms-5")) == 1


def test_review_request_no_gap_when_objective_set(proj, monkeypatch, capsys):
    # Give ms-5 an objective → strong enough, no gap warning.
    data = commands.load_project()
    next(m for m in data["milestones"] if m["id"] == "ms-5")["objective"] = "AC 明確"
    commands.save_project(data, op={"op": "test-set-objective"})
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_TARGET_ID", "ms-5")
    monkeypatch.setenv("BEACON_NEW_STATE", "done")
    monkeypatch.setenv("BEACON_INTENT", "本番稼働2週")
    commands.cmd_target_review_request()
    assert "完了条件の gap" not in capsys.readouterr().out


def test_milestone_done_default_applies_directly(proj, monkeypatch):
    # Non-breaking: without --review, done still applies immediately.
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_MS_ID", "ms-5")
    monkeypatch.setenv("BEACON_REASON", "done now")
    monkeypatch.setenv("BEACON_REVIEW", "0")
    commands.cmd_milestone_done()

    data = _reload(proj)
    ms = next(m for m in data["milestones"] if m["id"] == "ms-5")
    assert ms["status"] == "done"
    assert _entries(data, "ms-5") == []
