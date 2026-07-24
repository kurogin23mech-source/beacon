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
# Per-phase field set on advance (ms-124 e-4090).
# ---------------------------------------------------------------------------

def test_advance_sets_phase_field():
    data = _data()
    rec = te.create_target(data, CONTRACT, label="c",
                           fields={"counterparty": "X"})
    # the reviewer field belongs to legal_review; you can only set it on entry
    out, _, new = te.advance_target(data, CONTRACT, rec["id"],
                                    fields={"reviewer": "外部法律事務所"})
    assert new == "legal_review"
    assert out["reviewer"] == "外部法律事務所"


def test_advance_rejects_field_not_visible_at_new_phase():
    data = _data()
    rec = te.create_target(data, CONTRACT, label="c",
                           fields={"counterparty": "X"})
    # 'reviewer' is a legal_review field; it is NOT visible when we would land
    # on... actually create is at drafting; advancing to legal_review makes it
    # visible. Try setting a field that no phase declares.
    try:
        te.advance_target(data, CONTRACT, rec["id"],
                          fields={"ghostfield": "x"})
        assert False, "expected TargetEngineError"
    except te.TargetEngineError as e:
        assert "ghostfield" in str(e)


def test_advance_accepts_base_field_too():
    data = _data()
    rec = te.create_target(data, CONTRACT, label="c",
                           fields={"counterparty": "X"})
    # base fields stay visible at every phase — updatable on advance
    out, _, _ = te.advance_target(data, CONTRACT, rec["id"],
                                  fields={"note": "更新済"})
    assert out["note"] == "更新済"


REQ_PHASE = {
    "kind": "req_contract", "label": "必須付き契約", "profession": "backoffice",
    "type": "single-shot", "id_prefix": "rq-", "collection": "req_contracts",
    "fields": [{"key": "counterparty", "label": "相手方", "type": "string",
                "required": True}],
    "phases": [
        {"key": "drafting", "label": "起草"},
        {"key": "legal_review", "label": "法務レビュー",
         "fields": [{"key": "reviewer", "label": "レビュー依頼先",
                     "type": "string", "required": True}]},
        {"key": "signed", "label": "締結", "terminal": True},
    ],
}


def test_advance_enforces_required_phase_field():
    data = _data()
    rec = te.create_target(data, REQ_PHASE, label="c",
                           fields={"counterparty": "X"})
    # entering legal_review without its required 'reviewer' must fail
    try:
        te.advance_target(data, REQ_PHASE, rec["id"])
        assert False, "expected TargetEngineError for missing required field"
    except te.TargetEngineError as e:
        assert "reviewer" in str(e)
    # supplying it lets the advance through
    out, _, new = te.advance_target(data, REQ_PHASE, rec["id"],
                                    fields={"reviewer": "外部"})
    assert new == "legal_review"
    assert out["reviewer"] == "外部"


def test_required_phase_field_satisfied_by_prior_value():
    data = _data()
    rec = te.create_target(data, REQ_PHASE, label="c",
                           fields={"counterparty": "X"})
    te.advance_target(data, REQ_PHASE, rec["id"], fields={"reviewer": "外部"})
    te.advance_target(data, REQ_PHASE, rec["id"])                # → signed
    # kicked back to legal_review: reviewer already set, no re-supply needed
    out, _, new = te.advance_target(data, REQ_PHASE, rec["id"],
                                    to_phase="legal_review")
    assert new == "legal_review"
    assert out["reviewer"] == "外部"


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
    # Thick frame (ms-124 e-4089): a fresh target has the ball in our court and
    # its next move inferred as the phase after the initial one.
    assert proj == {"id": "ctr-1", "label": "A社 NDA", "status": "todo",
                    "kind": "contract", "work_items_total": 0,
                    "work_items_done": 0,
                    "detail": {"phase": "drafting", "type": "single-shot",
                               "who_has_the_ball": "self",
                               "next_move": "次フェーズへ進める: 弁護士レビュー"}}


# ---------------------------------------------------------------------------
# Thick cognitive frame (ms-124 e-4089) — WorkItems, Evidence, ball, next-move.
# ---------------------------------------------------------------------------

def test_work_items_inherited_and_counted():
    data = _data()
    rec = te.create_target(data, CONTRACT, label="c",
                           fields={"counterparty": "X"})
    # a fresh target starts with the empty arm (no longer projects hardcoded 0)
    assert te.list_work_items(rec) == []
    w1 = te.add_work_item(data, CONTRACT, rec["id"], "相手方に初稿を送る",
                          actor="claude")
    w2 = te.add_work_item(data, CONTRACT, rec["id"], "先方の赤入れを反映")
    assert w1["id"] == "ctr-1-w1"
    assert w2["id"] == "ctr-1-w2"
    assert w1["created_by"] == "claude"
    proj = te.project_target(CONTRACT, rec)
    assert (proj["work_items_total"], proj["work_items_done"]) == (2, 0)
    te.complete_work_item(data, CONTRACT, rec["id"], "ctr-1-w1", actor="claude",
                          reason="送付済")
    proj = te.project_target(CONTRACT, rec)
    assert (proj["work_items_total"], proj["work_items_done"]) == (2, 1)


def test_add_work_item_requires_description_and_known_target():
    data = _data()
    rec = te.create_target(data, CONTRACT, label="c",
                           fields={"counterparty": "X"})
    for bad in ("", "  "):
        try:
            te.add_work_item(data, CONTRACT, rec["id"], bad)
            assert False, "expected TargetEngineError"
        except te.TargetEngineError:
            pass
    try:
        te.add_work_item(data, CONTRACT, "ctr-99", "x")
        assert False
    except te.TargetEngineError:
        pass


def test_complete_unknown_work_item_raises():
    data = _data()
    rec = te.create_target(data, CONTRACT, label="c",
                           fields={"counterparty": "X"})
    try:
        te.complete_work_item(data, CONTRACT, rec["id"], "ctr-1-w9")
        assert False
    except te.TargetEngineError as e:
        assert "WorkItem" in str(e)


def test_evidence_links_to_work_item():
    data = _data()
    rec = te.create_target(data, CONTRACT, label="c",
                           fields={"counterparty": "X"})
    te.add_work_item(data, CONTRACT, rec["id"], "初稿送付")
    ev = te.add_evidence(data, CONTRACT, rec["id"], summary="メール送信済",
                         linked_id="ctr-1-w1", actor="claude")
    assert ev["id"] == "ctr-1-ev1"
    assert ev["linked_id"] == "ctr-1-w1"
    assert ev["summary"] == "メール送信済"
    assert te.list_evidence(rec) == [ev]


def test_evidence_rejects_unknown_linked_id():
    data = _data()
    rec = te.create_target(data, CONTRACT, label="c",
                           fields={"counterparty": "X"})
    try:
        te.add_evidence(data, CONTRACT, rec["id"], linked_id="ctr-1-w9")
        assert False
    except te.TargetEngineError as e:
        assert "linked_id" in str(e)


def test_evidence_without_link_is_allowed():
    data = _data()
    rec = te.create_target(data, CONTRACT, label="c",
                           fields={"counterparty": "X"})
    ev = te.add_evidence(data, CONTRACT, rec["id"], summary="キックオフ")
    assert ev["linked_id"] == ""


def test_set_ball_and_history():
    data = _data()
    rec = te.create_target(data, CONTRACT, label="c",
                           fields={"counterparty": "X"})
    assert rec["who_has_the_ball"] == "self"       # inherited default
    te.set_ball(data, CONTRACT, rec["id"], "counterpart", actor="claude",
                reason="先方レビュー待ち")
    assert rec["who_has_the_ball"] == "counterpart"
    ball_events = [h for h in rec["phase_history"] if h["kind"] == "ball_change"]
    assert ball_events[-1]["from"] == "self"
    assert ball_events[-1]["to"] == "counterpart"
    # 'none' clears it
    te.set_ball(data, CONTRACT, rec["id"], "none")
    assert rec["who_has_the_ball"] == ""


def test_set_ball_rejects_unknown():
    data = _data()
    rec = te.create_target(data, CONTRACT, label="c",
                           fields={"counterparty": "X"})
    try:
        te.set_ball(data, CONTRACT, rec["id"], "bogus")
        assert False
    except te.TargetEngineError as e:
        assert "bogus" in str(e)


def test_project_ball_tolerant_of_legacy_record():
    # a record written before the ball field reads as no-ball, not a crash
    legacy = {"id": "ctr-1", "kind": "contract", "phase": "drafting"}
    proj = te.project_target(CONTRACT, legacy)
    assert proj["detail"]["who_has_the_ball"] == ""


def test_infer_next_move_prefers_open_work_item():
    data = _data()
    rec = te.create_target(data, CONTRACT, label="c",
                           fields={"counterparty": "X"})
    te.add_work_item(data, CONTRACT, rec["id"], "初稿を書く")
    assert te.infer_next_move(CONTRACT, rec) == "WorkItem を進める: 初稿を書く"


def test_infer_next_move_advances_phase_when_no_open_items():
    data = _data()
    rec = te.create_target(data, CONTRACT, label="c",
                           fields={"counterparty": "X"})
    assert te.infer_next_move(CONTRACT, rec) == "次フェーズへ進める: 弁護士レビュー"


def test_infer_next_move_terminal_phase_suggests_close():
    data = _data()
    rec = te.create_target(data, CONTRACT, label="c",
                           fields={"counterparty": "X"})
    te.advance_target(data, CONTRACT, rec["id"])   # → legal_review
    te.advance_target(data, CONTRACT, rec["id"])   # → signed (terminal)
    assert "close" in te.infer_next_move(CONTRACT, rec)


def test_infer_next_move_empty_when_done():
    data = _data()
    rec = te.create_target(data, CONTRACT, label="c",
                           fields={"counterparty": "X"})
    te.close_target(data, CONTRACT, rec["id"])
    assert te.infer_next_move(CONTRACT, rec) == ""


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
