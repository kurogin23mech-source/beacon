"""Unit tests for `beacon opportunity anchor` — anchor_opportunity_gate (ms-144 e-5177).

The verb is the missing入口 that binds a non-meeting work item (act-/nrt-) — or a
meeting — as the発火源 of an opportunity's open前進ゲート, so 先方検討中 / 合意済み
gates (no ``kind:meeting`` template, no auto-anchor) can be linked by the AI.

These pin the four behaviours the leader裁定 approved (SPEC §設計方針 + caution 4):
  1. happy path: link + 遷移日 sync to the work item's date;
  2. idempotent (same) / permissive re-link (different, auditable);
  3. explicit error (not silent) when no gate is open;
  4. existence AND ownership guard — an unknown/foreign id is rejected so a
     「確定 (発火源 X)」 display is never a lie.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

import sales_entities as se  # noqa: E402


def _opp_with_open_gate():
    """A fresh opportunity sits in the funnel entry with an open前進ゲート."""
    data = se.build_sales_project("Acme Sales", "close deals")
    opp = se.opportunity_add(data, "Deal", created_at="T0")
    assert se.current_gate(data, opp) is not None
    return data, opp


# --- 1. happy path ---------------------------------------------------------

def test_anchor_links_activity_and_syncs_transition_date():
    data, opp = _opp_with_open_gate()
    act = se.activity_add(data, opp, "send proposal", deadline="2026-09-01")
    gate = se.anchor_opportunity_gate(data, opp, act, at="T1")
    assert gate["anchor"] == act
    # AC4: the gate's 遷移日 follows the anchor's date (the activity's deadline).
    assert gate["transition_date"] == "2026-09-01"
    assert se.current_gate(data, opp)["anchor"] == act
    # the link is recorded append-only on the gate history.
    assert any(h.get("action") == "anchored" and h.get("anchor") == act
               for h in gate.get("history", []))


def test_anchor_activity_without_deadline_does_not_clobber_transition_date():
    """A dateless activity anchor must not wipe an existing 遷移日 (safe-side
    guard — _work_item_date returns '' so anchor_gate leaves the date)."""
    data, opp = _opp_with_open_gate()
    se.set_transition_date(data, opp, "2026-08-15", at="T1")
    act = se.activity_add(data, opp, "call")  # no deadline
    gate = se.anchor_opportunity_gate(data, opp, act, at="T2")
    assert gate["anchor"] == act
    assert gate["transition_date"] == "2026-08-15"  # unchanged


# --- 2. idempotent / permissive re-link ------------------------------------

def test_anchor_same_work_item_is_idempotent_no_duplicate_history():
    data, opp = _opp_with_open_gate()
    act = se.activity_add(data, opp, "send proposal", deadline="2026-09-01")
    se.anchor_opportunity_gate(data, opp, act, at="T1")
    before = len(se.current_gate(data, opp).get("history", []))
    gate = se.anchor_opportunity_gate(data, opp, act, at="T2")  # same anchor
    assert gate["anchor"] == act
    assert len(gate.get("history", [])) == before  # no new row


def test_anchor_different_work_item_relinks_and_is_audited():
    data, opp = _opp_with_open_gate()
    a1 = se.activity_add(data, opp, "call", deadline="2026-09-01")
    a2 = se.activity_add(data, opp, "email", deadline="2026-09-05")
    se.anchor_opportunity_gate(data, opp, a1, at="T1")
    gate = se.anchor_opportunity_gate(data, opp, a2, at="T2")  # re-link
    assert gate["anchor"] == a2
    anchors = [h.get("anchor") for h in gate.get("history", [])
               if h.get("action") == "anchored"]
    assert anchors[-2:] == [a1, a2]  # both mis-link corrections auditable


# --- 3. no open gate → explicit error --------------------------------------

def test_anchor_raises_when_no_open_gate():
    data, opp = _opp_with_open_gate()
    act = se.activity_add(data, opp, "call", deadline="2026-09-01")
    # settle into a terminal phase → no gate open.
    se.jump_transition(data, opp, "不成立", at="T1")
    assert se.current_gate(data, opp) is None
    with pytest.raises(ValueError, match="no open advance gate"):
        se.anchor_opportunity_gate(data, opp, act, at="T2")


# --- 4. existence + ownership guard (leader caution 4) ---------------------

def test_anchor_rejects_unknown_work_item():
    data, opp = _opp_with_open_gate()
    with pytest.raises(ValueError, match="not found"):
        se.anchor_opportunity_gate(data, opp, "act-999", at="T1")


def test_anchor_rejects_foreign_work_item():
    """An activity that belongs to another商談 must not become this gate's
    発火源 — else 「確定 (発火源 X)」 would point at a work item this deal does
    not own (AX 上の毒)."""
    data, opp1 = _opp_with_open_gate()
    opp2 = se.opportunity_add(data, "Other Deal", created_at="T0")
    foreign = se.activity_add(data, opp2, "their call", deadline="2026-09-01")
    with pytest.raises(ValueError, match="belongs to"):
        se.anchor_opportunity_gate(data, opp1, foreign, at="T1")
    # the gate stayed empty — no lie written.
    assert (se.current_gate(data, opp1).get("anchor") or "") == ""


def test_anchor_rejects_non_work_item_id():
    data, opp = _opp_with_open_gate()
    with pytest.raises(ValueError, match="must be a work item"):
        se.anchor_opportunity_gate(data, opp, opp, at="T1")  # opp-id, not a work item
