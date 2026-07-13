"""Unit tests for the sales job-template entity model (ms-106 ①).

Covers the minimum data layer with the per-company phase funnel (案A):
Account (+nested Contact) with its own lifecycle phase, Opportunity with a
参照 association + config-driven funnel + append-only phase_history + terminal
rules, and Activity as a 従属 composition child. Also pins that a sales-template
project passes the shared ``core.validate_project`` unchanged.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

import core  # noqa: E402
import sales_entities as se  # noqa: E402


def _fresh():
    return se.build_sales_project("Acme Sales", "close deals")


# --- template + validator compat ------------------------------------------

def test_sales_template_shape():
    data = se.build_sales_project("S", "obj")
    assert data["profession"] == "sales"
    assert data["milestones"] == []
    assert data["opportunities"] == []
    assert data["accounts"] == []


def test_sales_template_seeds_phase_funnels():
    data = _fresh()
    assert [p["name"] for p in data["account_phases"]] == ["リード", "未成約顧客", "成約顧客"]
    opp_names = [p["name"] for p in data["opportunity_phases"]]
    assert opp_names == ["商談準備", "提案準備", "先方検討中", "合意済み", "成約", "失注", "不成立"]
    # terminals carry an outcome; stages carry allowed_terminals.
    won = se._find_phase_def(data["opportunity_phases"], "成約")
    assert won["terminal"] is True and won["outcome"] == "won"
    prep = se._find_phase_def(data["opportunity_phases"], "商談準備")
    assert prep["allowed_terminals"] == ["不成立"]


def test_sales_template_passes_shared_validator():
    core.validate_project(se.build_sales_project("S", "obj"))


# --- Account + lifecycle phase + Contact -----------------------------------

def test_account_add_defaults_to_first_phase():
    data = _fresh()
    a1 = se.account_add(data, "Globex", created_at="T0")
    assert a1 == "acc-1"
    acc = se.find_account(data, a1)
    assert acc["phase"] == "リード"
    assert acc["phase_history"] == [{"phase": "リード", "at": "T0", "note": "initial"}]


def test_account_name_required():
    data = _fresh()
    with pytest.raises(ValueError):
        se.account_add(data, "   ")


def test_account_phase_transition_is_append_only():
    data = _fresh()
    acc = se.account_add(data, "Globex", created_at="T0")
    se.phase_set(data, acc, "未成約顧客", at="T1")
    se.phase_set(data, acc, "成約顧客", at="T2")
    a = se.find_account(data, acc)
    assert a["phase"] == "成約顧客"
    assert [h["phase"] for h in a["phase_history"]] == ["リード", "未成約顧客", "成約顧客"]


def test_contact_nested_under_account():
    data = _fresh()
    acc = se.account_add(data, "Globex")
    se.contact_add(data, acc, "Alice", role="CTO", email="a@globex.com")
    contacts = se.find_account(data, acc)["contacts"]
    assert contacts == [{"name": "Alice", "role": "CTO", "email": "a@globex.com"}]


def test_contact_unknown_account():
    data = _fresh()
    with pytest.raises(ValueError):
        se.contact_add(data, "acc-99", "Alice")


# --- Opportunity + config-driven funnel ------------------------------------

def test_opportunity_add_defaults_to_funnel_entry():
    data = _fresh()
    opp = se.opportunity_add(data, "Big deal", created_at="T0")
    assert opp == "opp-1"
    o = se.find_opportunity(data, opp)
    assert o["phase"] == "商談準備"  # first non-terminal stage
    assert o["status"] == "open"
    assert o["phase_history"] == [{"phase": "商談準備", "at": "T0", "note": "initial"}]


def test_opportunity_reference_to_account_validated():
    data = _fresh()
    acc = se.account_add(data, "Globex")
    opp = se.opportunity_add(data, "Deal", account_id=acc)
    assert se.find_opportunity(data, opp)["account_id"] == acc


def test_opportunity_reference_unknown_account_rejected():
    data = _fresh()
    with pytest.raises(ValueError):
        se.opportunity_add(data, "Deal", account_id="acc-42")


def test_opportunity_rejects_bad_ball():
    data = _fresh()
    with pytest.raises(ValueError):
        se.opportunity_add(data, "Deal", who_has_the_ball="nobody")


# --- append-only phase transitions + terminal status -----------------------

def test_opportunity_phase_is_append_only():
    data = _fresh()
    opp = se.opportunity_add(data, "Deal", created_at="T0")
    se.phase_set(data, opp, "提案準備", note="proposal drafting", at="T1")
    se.phase_set(data, opp, "先方検討中", at="T2")
    o = se.find_opportunity(data, opp)
    assert o["phase"] == "先方検討中"
    assert [h["phase"] for h in o["phase_history"]] == ["商談準備", "提案準備", "先方検討中"]


def test_terminal_phase_mirrors_outcome_status():
    data = _fresh()
    for phase, expected in [("成約", "won"), ("失注", "lost"), ("不成立", "abandoned")]:
        opp = se.opportunity_add(data, f"Deal-{phase}")
        se.phase_set(data, opp, phase, at="T9")
        assert se.find_opportunity(data, opp)["status"] == expected


def test_phase_set_unknown_target_prefix():
    data = _fresh()
    with pytest.raises(ValueError):
        se.phase_set(data, "xyz-1", "成約")


def test_phase_set_unknown_opportunity():
    data = _fresh()
    with pytest.raises(ValueError):
        se.phase_set(data, "opp-9", "成約")


# --- terminal-rule warnings (non-blocking, SPEC §5) ------------------------

def test_no_warning_for_valid_terminal_from_proposal():
    data = _fresh()
    assert se.opportunity_phase_warnings(data, "提案準備", "成約") == []
    assert se.opportunity_phase_warnings(data, "合意済み", "失注") == []


def test_warning_when_terminal_violates_stage_rule():
    data = _fresh()
    # 商談準備 can only go 不成立; declaring 成約 from there warns.
    warns = se.opportunity_phase_warnings(data, "商談準備", "成約")
    assert warns and "不成立" in warns[0]


def test_warning_for_unknown_phase_name():
    data = _fresh()
    warns = se.opportunity_phase_warnings(data, "商談準備", "存在しない段階")
    assert warns and "語彙" in warns[0]


def test_warnings_do_not_block_declaration():
    data = _fresh()
    opp = se.opportunity_add(data, "Deal")
    # Even a rule-violating terminal is recorded (master=human declares).
    se.phase_set(data, opp, "成約", at="T1")  # from 商談準備 → rule violation
    assert se.find_opportunity(data, opp)["phase"] == "成約"


def test_account_phase_warning_unknown():
    data = _fresh()
    warns = se.account_phase_warnings(data, "存在しない")
    assert warns and "語彙" in warns[0]
    assert se.account_phase_warnings(data, "成約顧客") == []


# --- Activity 従属 composition ---------------------------------------------

def test_activity_add_under_opportunity():
    data = _fresh()
    opp = se.opportunity_add(data, "Deal")
    act = se.activity_add(data, opp, "send proposal", deadline="2026-08-01")
    assert act == "act-1"
    activities = se.find_opportunity(data, opp)["activities"]
    assert activities[0]["description"] == "send proposal"
    assert activities[0]["status"] == "todo"


def test_activity_ids_are_global_across_opportunities():
    data = _fresh()
    o1 = se.opportunity_add(data, "D1")
    o2 = se.opportunity_add(data, "D2")
    assert se.activity_add(data, o1, "call") == "act-1"
    assert se.activity_add(data, o2, "email") == "act-2"


def test_activity_unknown_opportunity():
    data = _fresh()
    with pytest.raises(ValueError):
        se.activity_add(data, "opp-9", "call")
