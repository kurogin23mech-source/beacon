"""目的達成レビューの backlog disposition ゲート (ms-119 / e-4579).

現状の目的達成レビュー (attainment) は SPEC 受入条件の機構がコードに在るかだけを見て
attained と判定でき、未着手の重要タスク (highest/high) を見落とす穴があった (ms-128
wave8 で「11/12 attained」と出たが 1 highest + 3 high タスク未着手だった=「掃除機が
ある≠掃除した」)。この一連は:

  * attainment は AC (アウトカム) 軸で定義し全タスク消化は強制しない (backlog は
    *定義* でなく *必須クロスチェック*);
  * 未着手 highest/high タスク全てに明示 disposition (done / superseded[理由必須] /
    blocks-attainment) が付くまで承認を refuse (choke point = core、CLI だけの
    guard でなく API 経路も塞ぐ);
  * disposition は verdict の前に台帳へ追記され、provenance (source) が grep 可能。
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # noqa: E402
import core  # noqa: E402
import review_spine  # noqa: E402
import transition_approval as _ta  # noqa: E402


# --- pure layer: task classification --------------------------------------

def _task(tid, *, status="todo", priority="high", ttype="task", desc="d"):
    return {"id": tid, "type": ttype, "status": status, "description": desc,
            "meta": {"priority": priority}}


def test_is_backlog_gated_only_unstarted_high_tier():
    assert _ta.is_backlog_gated(_task("e-1", status="todo", priority="highest"))
    assert _ta.is_backlog_gated(_task("e-2", status="todo", priority="high"))
    # started / done / cancelled tasks are not a silent miss
    assert not _ta.is_backlog_gated(_task("e-3", status="in_progress", priority="high"))
    assert not _ta.is_backlog_gated(_task("e-4", status="done", priority="highest"))
    assert not _ta.is_backlog_gated(_task("e-5", status="cancelled", priority="high"))
    # lower tiers are normal residue, not gated (attainment is outcome-defined)
    assert not _ta.is_backlog_gated(_task("e-6", status="todo", priority="medium"))
    assert not _ta.is_backlog_gated(_task("e-7", status="todo", priority="low"))
    # non-task entries never gate
    assert not _ta.is_backlog_gated(
        _task("e-8", status="todo", priority="high", ttype="commit"))


def test_normalize_task_status_buckets():
    assert _ta.normalize_task_status("todo") == "unstarted"
    assert _ta.normalize_task_status("") == "unstarted"
    assert _ta.normalize_task_status("done") == "done"
    assert _ta.normalize_task_status("closed") == "done"
    assert _ta.normalize_task_status("cancelled") == "cancelled"
    assert _ta.normalize_task_status("canceled") == "cancelled"
    for s in ("in_progress", "in_review", "waiting", "working", "leader_review",
              "user_review", "active"):
        assert _ta.normalize_task_status(s) == "in_progress"


def test_normalize_task_status_unknown_is_fail_closed():
    # #551 MUST-3: an unrecognised status must NOT slip past the gate as in_progress
    # (that was fail-OPEN). A gate guarding a completion claim fails CLOSED, so an
    # unknown status is treated as unstarted (= gated, demands a disposition).
    assert _ta.normalize_task_status("frobnicated") == "unstarted"
    assert _ta.normalize_task_status("weird-new-word") == "unstarted"
    # and such a task is therefore backlog-gated when highest/high
    assert _ta.is_backlog_gated(_task("e-x", status="frobnicated", priority="high"))


# --- pure layer: disposition records --------------------------------------

def _pending_entry(with_evidence=True):
    entry = _ta.build_transition_approval(
        entry_id="e-A", target_id="ms-T", target_kind="milestone",
        old_state="in_progress", new_state="observing", intent="claim",
        created_at="2026-01-01T00:00:00")
    if with_evidence:
        _ta.append_review_evidence(entry, verdict="attained", summary="AC met",
                                   source="judge", actor="a", at="t")
    return entry


def test_append_disposition_validates_verdict():
    e = _pending_entry()
    with pytest.raises(ValueError):
        _ta.append_disposition(e, task_id="e-1", verdict="skip", reason="",
                               source="", actor="a", at="t")
    assert e["meta"].get("dispositions", []) == []


def test_append_disposition_superseded_requires_reason():
    e = _pending_entry()
    with pytest.raises(ValueError):
        _ta.append_disposition(e, task_id="e-1", verdict="superseded", reason="",
                               source="", actor="a", at="t")
    # with a reason it records
    _ta.append_disposition(e, task_id="e-1", verdict="superseded",
                           reason="要件変更で不要に", source="judge", actor="a", at="t")
    assert e["meta"]["dispositions"][0]["verdict"] == "superseded"
    assert e["meta"]["dispositions"][0]["reason"] == "要件変更で不要に"


def test_disposition_map_latest_wins():
    e = _pending_entry()
    _ta.append_disposition(e, task_id="e-1", verdict="blocks-attainment",
                           reason="", source="", actor="a", at="t1")
    _ta.append_disposition(e, task_id="e-1", verdict="done",
                           reason="", source="", actor="a", at="t2")
    m = _ta.disposition_map(e)
    assert m["e-1"]["verdict"] == "done"  # latest wins
    # but the full ledger keeps both (audit)
    assert len(e["meta"]["dispositions"]) == 2


def test_undisposed_backlog_implicit_done_and_explicit_disposition():
    e = _pending_entry()
    backlog = [_task("e-1"), _task("e-2"), _task("e-3", status="done")]
    # e-3 is already done → implicitly disposed; e-1/e-2 still block
    und = _ta.undisposed_backlog(e, backlog)
    assert {t["id"] for t in und} == {"e-1", "e-2"}
    # dispose e-1 explicitly
    _ta.append_disposition(e, task_id="e-1", verdict="done", reason="",
                           source="", actor="a", at="t")
    und = _ta.undisposed_backlog(e, backlog)
    assert {t["id"] for t in und} == {"e-2"}


# --- core extractor --------------------------------------------------------

def test_unstarted_priority_tasks_walks_nested():
    target = {"id": "ms-T", "entries": [
        _task("e-1", priority="highest"),
        _task("e-2", status="done", priority="high"),
        {"id": "e-3", "type": "task", "status": "todo",
         "description": "parent", "meta": {"priority": "medium"}, "entries": [
             _task("e-4", priority="high"),  # nested gated
         ]},
    ]}
    got = {t["id"] for t in core.unstarted_priority_tasks(target)}
    assert got == {"e-1", "e-4"}


# --- core approve choke point ---------------------------------------------

def _data(entry, tasks=None):
    ms = {"id": "ms-T", "status": "in_progress", "title": "T",
          "entries": [entry] + list(tasks or [])}
    return {"milestones": [ms]}


def test_approve_refused_while_backlog_undisposed():
    # #551 MUST-1: no `target=` arg — the gate self-derives the target and runs on
    # every path.
    data = _data(_pending_entry(), tasks=[_task("e-1", priority="highest")])
    with pytest.raises(core.BacklogUndisposedError) as ex:
        core.target_transition_approval_approve(data, "e-A")
    assert {t["id"] for t in ex.value.undisposed} == {"e-1"}
    # verdict not recorded
    assert data["milestones"][0]["entries"][0]["status"] == "pending"


def test_approve_gate_runs_with_no_caller_opt_in():
    # #551 MUST-1: the earlier default-open `target=None` bypass is GONE. An approve
    # with an undisposed gated backlog must refuse even though the caller passes only
    # (data, entry_id) — there is no way to silently skip the gate by omitting an arg.
    data = _data(_pending_entry(), tasks=[_task("e-1", priority="highest")])
    with pytest.raises(core.BacklogUndisposedError):
        core.target_transition_approval_approve(data, "e-A")
    # the signature no longer accepts a `target` kwarg
    import inspect
    sig = inspect.signature(core.target_transition_approval_approve)
    assert "target" not in sig.parameters
    assert "allow_undisposed_backlog" in sig.parameters


def test_approve_proceeds_once_backlog_disposed():
    tasks = [_task("e-1", priority="highest")]
    data = _data(_pending_entry(), tasks=tasks)
    core.target_transition_approval_attach_disposition(
        data, "e-A", task_id="e-1", verdict="superseded", reason="不要になった",
        source="judge")
    entry, new_state = core.target_transition_approval_approve(data, "e-A")
    assert new_state == "observing"
    assert entry["status"] == "approved"


def test_approve_passes_with_no_gated_backlog():
    # A target with no unstarted highest/high tasks passes unchanged (only
    # lower-tier / done tasks present).
    tasks = [_task("e-1", priority="medium"), _task("e-2", status="done",
                                                    priority="high")]
    data = _data(_pending_entry(), tasks=tasks)
    entry, new_state = core.target_transition_approval_approve(data, "e-A")
    assert new_state == "observing"


def test_approve_skip_flag_stamps_audit_and_proceeds():
    # #551 MUST-1: an intentional skip is an EXPLICIT, audited flag (not a silent
    # default). allow_undisposed_backlog proceeds AND stamps backlog_check_skipped.
    data = _data(_pending_entry(), tasks=[_task("e-1", priority="highest")])
    entry, new_state = core.target_transition_approval_approve(
        data, "e-A", allow_undisposed_backlog=True, gate={})
    assert new_state == "observing"
    assert entry["meta"]["approval_gate"].get("backlog_check_skipped") is True


# --- #551 MUST-2: blocks-attainment refuse + stale-done warn ---------------

def test_approve_refused_when_task_marked_blocks_attainment():
    # A disposition of blocks-attainment must NOT satisfy the gate — approve refuses.
    tasks = [_task("e-1", priority="highest")]
    data = _data(_pending_entry(), tasks=tasks)
    core.target_transition_approval_attach_disposition(
        data, "e-A", task_id="e-1", verdict="blocks-attainment", source="judge")
    with pytest.raises(core.BacklogBlockedError) as ex:
        core.target_transition_approval_approve(data, "e-A")
    assert {t["id"] for t in ex.value.blocking} == {"e-1"}
    assert data["milestones"][0]["entries"][0]["status"] == "pending"


def test_blocks_attainment_can_be_skipped_with_ack():
    tasks = [_task("e-1", priority="highest")]
    data = _data(_pending_entry(), tasks=tasks)
    core.target_transition_approval_attach_disposition(
        data, "e-A", task_id="e-1", verdict="blocks-attainment", source="judge")
    entry, new_state = core.target_transition_approval_approve(
        data, "e-A", allow_undisposed_backlog=True, gate={})
    assert new_state == "observing"
    assert entry["meta"]["approval_gate"].get("backlog_check_skipped") is True


def test_stale_done_disposition_detected():
    # disposition=done but live status still unstarted → surfaced by helper (warn).
    e = _pending_entry()
    backlog = [_task("e-1", status="todo", priority="high")]
    _ta.append_disposition(e, task_id="e-1", verdict="done", reason="",
                           source="", actor="a", at="t")
    stale = _ta.stale_done_dispositions(e, backlog)
    assert {t["id"] for t in stale} == {"e-1"}
    # once the live status is done, it is no longer stale
    backlog2 = [_task("e-1", status="done", priority="high")]
    assert _ta.stale_done_dispositions(e, backlog2) == []


def test_attach_disposition_rejects_non_backlog_task():
    # A stray / typo'd id cannot fake-satisfy the gate for a still-blocking task.
    data = _data(_pending_entry(), tasks=[_task("e-1", priority="highest")])
    with pytest.raises(ValueError):
        core.target_transition_approval_attach_disposition(
            data, "e-A", task_id="e-999", verdict="done")


def test_attach_disposition_refuses_non_pending():
    entry = _pending_entry()
    entry["status"] = "approved"
    data = _data(entry, tasks=[_task("e-1", priority="highest")])
    with pytest.raises(ValueError):
        core.target_transition_approval_attach_disposition(
            data, "e-A", task_id="e-1", verdict="done")


# --- review context bundle carries the backlog ----------------------------

def test_attainment_bundle_carries_backlog():
    bundle = review_spine.assemble_attainment_context(
        target_id="ms-T", spec_origin_id="d1", spec_content="spec",
        criteria=[], target_ref="ms-T",
        backlog=[{"id": "e-1", "priority": "highest", "description": "d"}])
    assert bundle["backlog"] == [{"id": "e-1", "priority": "highest",
                                  "description": "d"}]
    # the judge contract instructs a per-task disposition
    assert "disposition" in bundle["judge_contract"]


def test_attainment_bundle_backlog_defaults_empty():
    bundle = review_spine.assemble_attainment_context(
        target_id="ms-T", spec_origin_id="", spec_content="",
        criteria=[], target_ref="ms-T")
    assert bundle["backlog"] == []


# --- CLI approve guard end-to-end -----------------------------------------

def _wire(monkeypatch, data):
    monkeypatch.setattr(commands, "load_project", lambda: data)
    monkeypatch.setattr(commands, "save_project", lambda *a, **k: None)
    monkeypatch.setattr(commands, "_apply_transition", lambda *a, **k: None)
    monkeypatch.setenv("BEACON_TARGET_APPROVE_USER_OVERRIDE", "1")
    monkeypatch.setenv("BEACON_ENTRY_ID", "e-A")
    monkeypatch.delenv("BEACON_ACK_NO_EVIDENCE", raising=False)
    monkeypatch.delenv("BEACON_ACK_UNDISPOSED_BACKLOG", raising=False)
    monkeypatch.delenv("BEACON_RATIONALE", raising=False)


def test_cli_approve_refused_while_backlog_undisposed(monkeypatch):
    data = _data(_pending_entry(), tasks=[_task("e-1", priority="highest")])
    _wire(monkeypatch, data)
    with pytest.raises(SystemExit) as ex:
        commands.cmd_target_approve()
    assert ex.value.code == 2
    assert data["milestones"][0]["entries"][0]["status"] == "pending"


def test_cli_approve_proceeds_after_disposition(monkeypatch):
    data = _data(_pending_entry(), tasks=[_task("e-1", priority="highest")])
    core.target_transition_approval_attach_disposition(
        data, "e-A", task_id="e-1", verdict="done", source="judge")
    _wire(monkeypatch, data)
    commands.cmd_target_approve()  # no SystemExit
    assert data["milestones"][0]["entries"][0]["status"] == "approved"


def test_cli_approve_refused_when_blocks_attainment(monkeypatch):
    # #551 MUST-2 at the CLI: a blocks-attainment disposition refuses (exit 2).
    data = _data(_pending_entry(), tasks=[_task("e-1", priority="highest")])
    core.target_transition_approval_attach_disposition(
        data, "e-A", task_id="e-1", verdict="blocks-attainment", source="judge")
    _wire(monkeypatch, data)
    with pytest.raises(SystemExit) as ex:
        commands.cmd_target_approve()
    assert ex.value.code == 2
    assert data["milestones"][0]["entries"][0]["status"] == "pending"


def test_cli_approve_skip_flag_proceeds(monkeypatch):
    # #551 MUST-1 at the CLI: --acknowledge-undisposed-backlog is the explicit,
    # audited skip (env BEACON_ACK_UNDISPOSED_BACKLOG=1).
    data = _data(_pending_entry(), tasks=[_task("e-1", priority="highest")])
    _wire(monkeypatch, data)
    monkeypatch.setenv("BEACON_ACK_UNDISPOSED_BACKLOG", "1")
    commands.cmd_target_approve()  # no SystemExit
    entry = data["milestones"][0]["entries"][0]
    assert entry["status"] == "approved"
    assert entry["meta"]["approval_gate"].get("backlog_check_skipped") is True


# --- #551 SHOULD-1: --disposition flag + --verdict migration alias ---------

def _wire_disp(monkeypatch, data):
    monkeypatch.setattr(commands, "load_project", lambda: data)
    monkeypatch.setattr(commands, "save_project", lambda *a, **k: None)
    monkeypatch.setenv("BEACON_ENTRY_ID", "e-A")
    monkeypatch.setenv("BEACON_DISP_TASK", "e-1")
    for v in ("BEACON_DISP_DISPOSITION", "BEACON_DISP_VERDICT",
              "BEACON_DISP_REASON", "BEACON_DISP_SOURCE"):
        monkeypatch.delenv(v, raising=False)


def test_cli_attach_disposition_new_flag(monkeypatch):
    data = _data(_pending_entry(), tasks=[_task("e-1", priority="highest")])
    _wire_disp(monkeypatch, data)
    monkeypatch.setenv("BEACON_DISP_DISPOSITION", "done")
    commands.cmd_target_attach_disposition()
    disp = _ta.disposition_map(data["milestones"][0]["entries"][0])
    assert disp["e-1"]["verdict"] == "done"


def test_cli_attach_disposition_verdict_alias(monkeypatch):
    # --verdict still accepted as a migration alias.
    data = _data(_pending_entry(), tasks=[_task("e-1", priority="highest")])
    _wire_disp(monkeypatch, data)
    monkeypatch.setenv("BEACON_DISP_VERDICT", "done")
    commands.cmd_target_attach_disposition()
    disp = _ta.disposition_map(data["milestones"][0]["entries"][0])
    assert disp["e-1"]["verdict"] == "done"


def test_cli_attach_disposition_conflicting_flags_refused(monkeypatch):
    data = _data(_pending_entry(), tasks=[_task("e-1", priority="highest")])
    _wire_disp(monkeypatch, data)
    monkeypatch.setenv("BEACON_DISP_DISPOSITION", "done")
    monkeypatch.setenv("BEACON_DISP_VERDICT", "superseded")
    with pytest.raises(SystemExit) as ex:
        commands.cmd_target_attach_disposition()
    assert ex.value.code == 1
