"""Tests for lib/target_engine.py — generic descriptor-driven target mechanics
(ms-122 e-3956): create / advance-phase / close / list a data-defined
target-class, delegating primitives to work_base / work_model."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import target_engine as te  # noqa: E402
import work_model as wm  # noqa: E402


CONTRACT = {
    "kind": "contract",
    "label": "契約",
    "profession": "backoffice",
    "type": "single-shot",
    "id_prefix": "ctr-",
    "collection": "contracts",
    "fields": [
        {"key": "counterparty", "label": "相手方", "type": "string",
         "required": True},
        {"key": "note", "label": "備考", "type": "text"},
    ],
    "phases": [
        {"key": "drafting", "label": "起草"},
        {"key": "legal_review", "label": "弁護士レビュー",
         "fields": [{"key": "reviewer", "label": "レビュー依頼先",
                     "type": "string"}]},
        {"key": "signed", "label": "締結", "terminal": True},
    ],
}


def _data():
    return {"name": "t"}


# ---------------------------------------------------------------------------
# Create.
# ---------------------------------------------------------------------------

def test_create_allocates_id_and_initial_phase():
    data = _data()
    rec = te.create_target(data, CONTRACT, label="A社 NDA",
                           fields={"counterparty": "A社"}, actor="claude")
    assert rec["id"] == "ctr-1"
    assert rec["kind"] == "contract"
    assert rec["phase"] == "drafting"          # first declared phase
    assert rec["status"] == wm.TODO_STATUS
    assert wm.target_label(rec) == "A社 NDA"
    assert rec["counterparty"] == "A社"
    assert rec["created_by"] == "claude"
    # stored in the descriptor's collection
    assert data["contracts"] == [rec]


def test_create_increments_id():
    data = _data()
    te.create_target(data, CONTRACT, label="1", fields={"counterparty": "X"})
    r2 = te.create_target(data, CONTRACT, label="2", fields={"counterparty": "Y"})
    assert r2["id"] == "ctr-2"


def test_create_rejects_unknown_field():
    data = _data()
    try:
        te.create_target(data, CONTRACT, label="x",
                         fields={"counterparty": "X", "bogus": 1})
        assert False, "expected TargetEngineError"
    except te.TargetEngineError as e:
        assert "bogus" in str(e)


def test_create_requires_required_base_field():
    data = _data()
    try:
        te.create_target(data, CONTRACT, label="x", fields={})
        assert False, "expected TargetEngineError"
    except te.TargetEngineError as e:
        assert "counterparty" in str(e)


def test_create_requires_label():
    try:
        te.create_target(_data(), CONTRACT, label="  ",
                         fields={"counterparty": "X"})
        assert False
    except te.TargetEngineError:
        pass


# ---------------------------------------------------------------------------
# Advance.
# ---------------------------------------------------------------------------

def test_advance_moves_to_next_phase_and_records_history():
    data = _data()
    rec = te.create_target(data, CONTRACT, label="c",
                           fields={"counterparty": "X"}, actor="claude")
    out, old, new = te.advance_target(data, CONTRACT, rec["id"], actor="claude",
                                      reason="起草完了")
    assert (old, new) == ("drafting", "legal_review")
    assert out["phase"] == "legal_review"
    hist = out["phase_history"]
    assert len(hist) == 1
    assert hist[0]["kind"] == "phase_change"
    assert hist[0]["from"] == "drafting"
    assert hist[0]["to"] == "legal_review"
    assert hist[0]["reason"] == "起草完了"
    assert hist[0]["actor"] == "claude"


def test_advance_to_explicit_phase_allows_moving_back():
    data = _data()
    rec = te.create_target(data, CONTRACT, label="c",
                           fields={"counterparty": "X"})
    te.advance_target(data, CONTRACT, rec["id"])                 # → legal_review
    te.advance_target(data, CONTRACT, rec["id"])                 # → signed
    out, old, new = te.advance_target(data, CONTRACT, rec["id"],
                                      to_phase="legal_review")   # kicked back
    assert (old, new) == ("signed", "legal_review")
    assert len(out["phase_history"]) == 3


def test_advance_past_final_phase_raises():
    data = _data()
    rec = te.create_target(data, CONTRACT, label="c",
                           fields={"counterparty": "X"})
    te.advance_target(data, CONTRACT, rec["id"])   # → legal_review
    te.advance_target(data, CONTRACT, rec["id"])   # → signed (final)
    try:
        te.advance_target(data, CONTRACT, rec["id"])
        assert False, "expected TargetEngineError at final phase"
    except te.TargetEngineError as e:
        assert "最終 phase" in str(e)


def test_advance_unknown_phase_raises():
    data = _data()
    rec = te.create_target(data, CONTRACT, label="c",
                           fields={"counterparty": "X"})
    try:
        te.advance_target(data, CONTRACT, rec["id"], to_phase="ghost")
        assert False
    except te.TargetEngineError as e:
        assert "ghost" in str(e)


def test_advance_unknown_target_raises():
    try:
        te.advance_target(_data(), CONTRACT, "ctr-99")
        assert False
    except te.TargetEngineError:
        pass


def test_is_terminal_phase():
    assert te.is_terminal_phase(CONTRACT, "signed") is True
    assert te.is_terminal_phase(CONTRACT, "drafting") is False


# ---------------------------------------------------------------------------
# Close.
# ---------------------------------------------------------------------------

def test_close_marks_done():
    data = _data()
    rec = te.create_target(data, CONTRACT, label="c",
                           fields={"counterparty": "X"})
    out = te.close_target(data, CONTRACT, rec["id"], actor="claude",
                          reason="締結完了")
    assert wm.is_done(out)
    assert out["meta"]["done_by"] == "claude"
    assert out["meta"]["done_reason"] == "締結完了"


# ---------------------------------------------------------------------------
# List / find / project.
# ---------------------------------------------------------------------------

def test_list_and_find():
    data = _data()
    te.create_target(data, CONTRACT, label="1", fields={"counterparty": "X"})
    te.create_target(data, CONTRACT, label="2", fields={"counterparty": "Y"})
    assert [r["id"] for r in te.list_targets(data, CONTRACT)] == ["ctr-1", "ctr-2"]
    assert te.find_target(data, CONTRACT, "ctr-2")["label"] == "2"
    assert te.find_target(data, CONTRACT, "ctr-9") is None


def test_list_empty_when_no_collection():
    assert te.list_targets(_data(), CONTRACT) == []


def test_project_target_shape():
    data = _data()
    rec = te.create_target(data, CONTRACT, label="A社 NDA",
                           fields={"counterparty": "A社"})
    proj = te.project_target(CONTRACT, rec)
    assert proj == {"id": "ctr-1", "label": "A社 NDA", "status": "todo",
                    "kind": "contract", "work_items_total": 0,
                    "work_items_done": 0,
                    "detail": {"phase": "drafting", "type": "single-shot"}}


# ---------------------------------------------------------------------------
# Phase-less class (persistent target with no phases).
# ---------------------------------------------------------------------------

MONTHLY_CLOSE = {
    "kind": "monthly_close", "label": "月次決算", "profession": "backoffice",
    "type": "persistent", "id_prefix": "mc-", "collection": "monthly_closes",
    "fields": [], "phases": [],
}


def test_phaseless_create_has_no_phase():
    data = _data()
    rec = te.create_target(data, MONTHLY_CLOSE, label="2026-07")
    assert "phase" not in rec or rec.get("phase") == ""


def test_phaseless_advance_raises():
    data = _data()
    rec = te.create_target(data, MONTHLY_CLOSE, label="2026-07")
    try:
        te.advance_target(data, MONTHLY_CLOSE, rec["id"])
        assert False
    except te.TargetEngineError as e:
        assert "phase" in str(e)
