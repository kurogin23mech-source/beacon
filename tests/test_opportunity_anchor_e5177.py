"""Unit tests for `beacon opportunity anchor` — anchor_opportunity_gate (ms-144 e-5177).

The verb is the missing入口 that binds a non-meeting work item (act-) — or a
meeting — as the発火源 of an opportunity's open前進ゲート, so 先方検討中 / 合意済み
gates (no ``kind:meeting`` template, no auto-anchor) can be linked by the AI.

These pin the behaviours the leader裁定 + PR #636 independent review approved:
  1. happy path: link + 遷移日 sync to the work item's date;
  2. idempotent (same, changed=False) / permissive re-link (different, auditable);
  3. explicit error (not silent) when no gate is open;
  4. existence AND ownership guard — an unknown/foreign id is rejected so a
     「確定 (発火源 X)」 display is never a lie (leader caution 4);
  5. meeting/activity only — nurturing (nrt-, Account-scoped) is rejected with a
     recovery hint, not a confusing ownership error (review A2);
  6. the return reports only what actually happened (review A3): changed/synced.
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
    gate, changed, synced = se.anchor_opportunity_gate(data, opp, act, at="T1")
    assert changed is True
    assert gate["anchor"] == act
    # AC4: the gate's 遷移日 follows the anchor's date (the activity's deadline),
    # and that sync is reported as having happened this call.
    assert gate["transition_date"] == "2026-09-01"
    assert synced == "2026-09-01"
    assert se.current_gate(data, opp)["anchor"] == act
    # the link is recorded append-only on the gate history.
    assert any(h.get("action") == "anchored" and h.get("anchor") == act
               for h in gate.get("history", []))


def test_anchor_activity_without_deadline_does_not_clobber_transition_date():
    """A dateless activity anchor must not wipe an existing 遷移日 (safe-side
    guard), and must not claim a sync happened (review A3)."""
    data, opp = _opp_with_open_gate()
    se.set_transition_date(data, opp, "2026-08-15", at="T1")
    act = se.activity_add(data, opp, "call")  # no deadline
    gate, changed, synced = se.anchor_opportunity_gate(data, opp, act, at="T2")
    assert changed is True
    assert gate["anchor"] == act
    assert gate["transition_date"] == "2026-08-15"  # unchanged
    assert synced == ""  # nothing synced — do not lie about it


# --- 2. idempotent / permissive re-link ------------------------------------

def test_anchor_same_work_item_is_idempotent_and_reports_no_change():
    data, opp = _opp_with_open_gate()
    act = se.activity_add(data, opp, "send proposal", deadline="2026-09-01")
    se.anchor_opportunity_gate(data, opp, act, at="T1")
    before = len(se.current_gate(data, opp).get("history", []))
    gate, changed, synced = se.anchor_opportunity_gate(data, opp, act, at="T2")
    assert changed is False  # A3: report the truth — nothing changed
    assert synced == ""
    assert gate["anchor"] == act
    assert len(gate.get("history", [])) == before  # no new row


def test_anchor_different_work_item_relinks_and_is_audited():
    data, opp = _opp_with_open_gate()
    a1 = se.activity_add(data, opp, "call", deadline="2026-09-01")
    a2 = se.activity_add(data, opp, "email", deadline="2026-09-05")
    se.anchor_opportunity_gate(data, opp, a1, at="T1")
    gate, changed, synced = se.anchor_opportunity_gate(data, opp, a2, at="T2")  # re-link
    assert changed is True
    assert gate["anchor"] == a2
    assert synced == "2026-09-05"  # 遷移日 followed the new anchor
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


# --- 5. meeting/activity only — nurturing rejected (review A2) --------------

def test_anchor_rejects_nurturing_with_recovery_hint():
    """A nurturing (nrt-) lives under an Account, never an Opportunity, so it
    can never own an opportunity gate — reject it with a clear recovery hint
    rather than a confusing ownership error."""
    data, opp = _opp_with_open_gate()
    acc = se.account_add(data, "Globex", created_at="T0")
    nrt = se.nurturing_add(data, acc, "quarterly check-in")
    with pytest.raises(ValueError, match="nurturing"):
        se.anchor_opportunity_gate(data, opp, nrt, at="T1")
    assert (se.current_gate(data, opp).get("anchor") or "") == ""


def test_anchor_rejects_non_work_item_id():
    data, opp = _opp_with_open_gate()
    with pytest.raises(ValueError, match="meeting or activity"):
        se.anchor_opportunity_gate(data, opp, opp, at="T1")  # opp-id, not a work item
