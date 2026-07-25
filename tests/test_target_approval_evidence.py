"""target-close 承認は独立レビュー証拠の消費を前提にする (ms-119 / e-4205).

e-4005 は「目的達成の証拠は実装者の self-report であってはならない」を掲げたが、CLI
経路に穴が残り、独立 judge を回さずとも承認依頼の作成・承認が通っていた (e-4198 で
実証)。この一連は「独立証拠なしの SILENT 承認」を構造で塞ぐ:
  * 承認 entry に独立証拠スロット (review_evidence) を持たせる;
  * `beacon target approve` は証拠が空なら refuse し、`--acknowledge-no-evidence`
    という明示 ack でのみ通す (人間の verdict 所有は奪わず、沈黙だけを禁じる);
  * ack で通した事実は gate に記録され監査可能。
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # noqa: E402
import core  # noqa: E402
import transition_approval as _ta  # noqa: E402


# --- pure layer ------------------------------------------------------------

def _pending_entry(with_evidence=False):
    entry = _ta.build_transition_approval(
        entry_id="e-1", target_id="ms-T", target_kind="milestone",
        old_state="in_progress", new_state="observing", intent="claim",
        created_at="2026-01-01T00:00:00")
    if with_evidence:
        _ta.append_review_evidence(entry, verdict="attained", summary="全 AC met",
                                   source="independent-judge:fable", actor="a",
                                   at="2026-01-01T00:00:01")
    return entry


def test_build_carries_empty_review_evidence_slot():
    e = _pending_entry()
    assert e["meta"]["review_evidence"] == []
    assert _ta.has_independent_evidence(e) is False


def test_append_review_evidence_is_observable():
    e = _pending_entry()
    _ta.append_review_evidence(e, verdict="attained", summary="s", source="j",
                               actor="a", at="t")
    rec = e["meta"]["review_evidence"][0]
    assert rec == {"verdict": "attained", "summary": "s", "source": "j",
                   "actor": "a", "at": "t"}
    assert _ta.has_independent_evidence(e) is True


# --- core attach -----------------------------------------------------------

def _data(entry):
    return {"milestones": [{"id": "ms-T", "status": "in_progress", "title": "T",
                            "entries": [entry]}]}


def test_core_attach_evidence_records_on_pending():
    data = _data(_pending_entry())
    entry = core.target_transition_approval_attach_evidence(
        data, "e-1", verdict="partial", summary="AC3 未達", source="judge")
    assert entry["meta"]["review_evidence"][0]["verdict"] == "partial"


def test_core_attach_evidence_refuses_non_pending():
    entry = _pending_entry()
    entry["status"] = "approved"  # already terminal
    data = _data(entry)
    with pytest.raises(ValueError):
        core.target_transition_approval_attach_evidence(
            data, "e-1", verdict="attained", summary="s")


# --- CLI approve guard -----------------------------------------------------

def _wire(monkeypatch, data):
    monkeypatch.setattr(commands, "load_project", lambda: data)
    monkeypatch.setattr(commands, "save_project", lambda *a, **k: None)
    # isolate the guard from transition-application machinery
    monkeypatch.setattr(commands, "_apply_transition", lambda *a, **k: None)
    # pass the e-4006 AI-approve ban so the e-4205 evidence guard is what we test
    monkeypatch.setenv("BEACON_TARGET_APPROVE_USER_OVERRIDE", "1")
    monkeypatch.setenv("BEACON_ENTRY_ID", "e-1")
    monkeypatch.delenv("BEACON_ACK_NO_EVIDENCE", raising=False)
    monkeypatch.delenv("BEACON_RATIONALE", raising=False)


def test_approve_refused_without_independent_evidence(monkeypatch):
    data = _data(_pending_entry(with_evidence=False))
    _wire(monkeypatch, data)
    with pytest.raises(SystemExit) as ex:
        commands.cmd_target_approve()
    assert ex.value.code == 2
    # verdict not recorded — the transition did not pass the guard
    assert data["milestones"][0]["entries"][0]["status"] == "pending"


def test_approve_proceeds_with_independent_evidence(monkeypatch):
    data = _data(_pending_entry(with_evidence=True))
    _wire(monkeypatch, data)
    commands.cmd_target_approve()  # no SystemExit
    assert data["milestones"][0]["entries"][0]["status"] == "approved"


def test_approve_proceeds_with_explicit_ack_and_audits_it(monkeypatch):
    data = _data(_pending_entry(with_evidence=False))
    _wire(monkeypatch, data)
    monkeypatch.setenv("BEACON_ACK_NO_EVIDENCE", "1")
    commands.cmd_target_approve()  # no SystemExit — conscious bypass
    entry = data["milestones"][0]["entries"][0]
    assert entry["status"] == "approved"
    # the conscious bypass leaves an auditable footprint on the gate
    assert entry["meta"]["approval_gate"]["no_evidence_ack"] is True
