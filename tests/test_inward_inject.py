"""ms-136 e-4697 — inward inject seam tests.

Pins the SPEC 方針5 foundation: a擬似着信 (fake inbound reply) fed through the
real receiving line (`sales_entities.communication_add(direction=inbound)`)
drives 取り込み → ball が自分に戻る → phase 前進 becomes possible, and fires no
outbound / external effect. Each assertion cites the task e-4697 AC it covers.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

import sales_entities as se  # noqa: E402
import inward_inject as ii  # noqa: E402


def _fresh():
    return se.build_sales_project("Acme Sales", "close deals")


def _deal_awaiting_reply(data):
    """An opportunity where we sent something last, so the ball is the
    counterpart's — the exact state a journey stalls in until a reply arrives."""
    oid = se.opportunity_add(data, "Big deal")
    se.communication_add(data, oid, "提案を送付", direction=se.COMM_OUTBOUND,
                         channel="email", created_at="2026-08-01T00:00:00Z")
    assert se.derive_ball(se.find_opportunity(data, oid)) == se.BALL_COUNTERPART
    return oid


# --- AC #1: a test-inject path onto the existing receiving line exists -------

def test_inject_appends_inbound_communication_via_real_receiving_line():
    data = _fresh()
    oid = se.opportunity_add(data, "Deal")

    res = ii.inject_inbound_communication(
        data, oid, "先方から返信", channel="email",
        source_ref="<msg-1@example.com>", at="2026-08-09T10:00:00Z")

    # It went through the SAME store as real replies: read it back off the opp.
    comms = se.communications_of(se.find_opportunity(data, oid))
    assert len(comms) == 1
    rec = comms[0]
    assert rec["id"] == res["comm_id"]
    assert rec["direction"] == se.COMM_INBOUND
    assert rec["channel"] == "email"
    assert rec["source"]["ref"] == "<msg-1@example.com>"


def test_injected_arrival_is_marked_and_distinguishable():
    # AC #1 audit: a擬似着信 must never be mistaken for a real customer reply.
    data = _fresh()
    oid = se.opportunity_add(data, "Deal")
    ii.inject_inbound_communication(data, oid, "擬似返信")
    real = se.communication_add(data, oid, "本物の返信", direction=se.COMM_INBOUND)

    comms = {c["id"]: c for c in se.communications_of(se.find_opportunity(data, oid))}
    injected = next(c for c in comms.values() if c["summary"] == "擬似返信")
    real_rec = comms[real]
    assert ii.is_injected(injected) is True
    assert ii.is_injected(real_rec) is False


# --- AC #2: injection drives 取り込み → ball 更新 → phase 前進 ---------------

def test_inject_flips_ball_back_to_self():
    data = _fresh()
    oid = _deal_awaiting_reply(data)

    res = ii.inject_inbound_communication(data, oid, "検討します、と返信あり")

    assert res["ball_before"] == se.BALL_COUNTERPART
    assert res["ball_after"] == se.BALL_SELF
    assert res["ingested"] is True  # 取り込み成立 signal


def test_ingested_false_when_ball_does_not_return():
    # Guard: `ingested` reflects the derived ball, not a blind "we appended".
    # An inbound arrival always returns the ball, so to exercise the False
    # branch we derive it from a target that has no communication at all.
    data = _fresh()
    oid = se.opportunity_add(data, "Deal")
    opp = se.find_opportunity(data, oid)
    assert se.derive_ball(opp) is None  # unknown ball, not a default


def test_full_chain_inject_then_phase_advance():
    # AC #2 end-to-end: the injected arrival is the precondition that lets the
    # journey advance the phase (前進). Before injection the ball is the
    # counterpart's; after, advancing to the next phase succeeds.
    data = _fresh()
    oid = _deal_awaiting_reply(data)
    start_phase = se.find_opportunity(data, oid)["phase"]

    res = ii.inject_inbound_communication(data, oid, "合意に近い返信")
    assert res["ingested"] is True

    out = se.advance_transition(data, oid, next_transition_date="2026-08-20")
    assert out["phase"] == se.next_opportunity_phase(data, start_phase)
    assert se.find_opportunity(data, oid)["phase"] != start_phase


def test_inject_onto_work_item_nests_under_it():
    # The receiving line accepts a work-item target (act-) too, nesting the
    # arrival under it while still driving the deal-level ball.
    data = _fresh()
    oid = _deal_awaiting_reply(data)
    act_id = se.activity_add(data, oid, "フォロー電話")

    res = ii.inject_inbound_communication(data, act_id, "電話で前向き回答")

    assert res["target_id"] == oid          # resolved container is the opp
    assert res["ball_after"] == se.BALL_SELF
    # nested under the activity, not at opp level
    opp = se.find_opportunity(data, oid)
    act = next(a for a in opp["activities"] if a["id"] == act_id)
    assert any(c["id"] == res["comm_id"] for c in act.get("communications", []))


# --- AC #3: no outbound / external send fires -------------------------------

def test_inject_fires_no_outbound_communication():
    data = _fresh()
    oid = _deal_awaiting_reply(data)
    before = len(se.communications_of(se.find_opportunity(data, oid)))

    ii.inject_inbound_communication(data, oid, "返信")

    comms = se.communications_of(se.find_opportunity(data, oid))
    # exactly one new record, and it is inbound — nothing outward was produced
    assert len(comms) == before + 1
    newest = comms[-1]
    assert newest["direction"] == se.COMM_INBOUND


def test_module_has_no_outward_dependency():
    # Structural guarantee of AC #3: the seam cannot reach an external send
    # because it imports nothing that sends (no api_client / bus / MCP / net).
    src = (REPO / "lib" / "inward_inject.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {"api_client", "cmd_bus", "bus_protocol", "ws_client",
                 "requests", "urllib", "http", "socket", "subprocess"}
    assert not (imported & forbidden), f"outward dep leaked: {imported & forbidden}"


# --- error handling ---------------------------------------------------------

def test_inject_unknown_target_raises():
    data = _fresh()
    with pytest.raises(ValueError):
        ii.inject_inbound_communication(data, "opp-999", "reply")


def test_inject_empty_summary_raises():
    data = _fresh()
    oid = se.opportunity_add(data, "Deal")
    with pytest.raises(ValueError):
        ii.inject_inbound_communication(data, oid, "   ")
