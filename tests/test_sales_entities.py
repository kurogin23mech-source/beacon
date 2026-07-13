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


# --- deletion + referential integrity --------------------------------------

def test_opportunity_delete_removes_it_and_activities():
    data = _fresh()
    opp = se.opportunity_add(data, "Deal")
    se.activity_add(data, opp, "call")
    se.opportunity_delete(data, opp)
    assert se.find_opportunity(data, opp) is None
    assert data["opportunities"] == []


def test_opportunity_delete_unknown():
    data = _fresh()
    with pytest.raises(ValueError):
        se.opportunity_delete(data, "opp-9")


def test_account_delete_unreferenced():
    data = _fresh()
    acc = se.account_add(data, "Globex")
    orphaned = se.account_delete(data, acc)
    assert orphaned == []
    assert se.find_account(data, acc) is None


def test_account_delete_referenced_refused_without_force():
    data = _fresh()
    acc = se.account_add(data, "Globex")
    se.opportunity_add(data, "Deal", account_id=acc)
    with pytest.raises(ValueError):
        se.account_delete(data, acc)
    # account survives the refusal
    assert se.find_account(data, acc) is not None


def test_account_delete_force_orphans_opportunities():
    data = _fresh()
    acc = se.account_add(data, "Globex")
    opp = se.opportunity_add(data, "Deal", account_id=acc)
    orphaned = se.account_delete(data, acc, force=True)
    assert orphaned == [opp]
    assert se.find_account(data, acc) is None
    # the deal survives, now account-less (a lost deal shouldn't vanish)
    assert se.find_opportunity(data, opp)["account_id"] is None


def test_account_delete_unknown():
    data = _fresh()
    with pytest.raises(ValueError):
        se.account_delete(data, "acc-9")


# --- send identity pin (複垢取り違え防止) -----------------------------------

def test_send_identity_default_empty():
    assert se.get_send_identity(_fresh()) == ""


def test_send_identity_set_and_get():
    data = _fresh()
    se.set_send_identity(data, "sales@corp.example")
    assert se.get_send_identity(data) == "sales@corp.example"


def test_send_identity_set_requires_value():
    data = _fresh()
    with pytest.raises(ValueError):
        se.set_send_identity(data, "   ")


def test_check_send_from_no_pin_blocks():
    ok, msg = se.check_send_from(_fresh(), "anyone@corp.example")
    assert ok is False and "未設定" in msg


def test_check_send_from_empty_from_blocks():
    data = _fresh()
    se.set_send_identity(data, "sales@corp.example")
    ok, msg = se.check_send_from(data, "")
    assert ok is False and "空" in msg


def test_check_send_from_match_ok():
    data = _fresh()
    se.set_send_identity(data, "Sales@Corp.Example")
    ok, _ = se.check_send_from(data, "sales@corp.example")  # case-insensitive
    assert ok is True


def test_check_send_from_mismatch_blocks():
    data = _fresh()
    se.set_send_identity(data, "sales@corp.example")
    ok, msg = se.check_send_from(data, "personal@gmail.example")
    assert ok is False and "取り違え" in msg


# --- send-account ledger (ms-107 e-3365) -----------------------------------

def _with_ledger():
    """Sales project with two send accounts + routes, default = 会社."""
    data = _fresh()
    se.add_send_account(data, "会社", "sales@corp.example", routes={
        "gmail": {"namespace": "mcp__gmail"},
        "calendar": {"namespace": "mcp__google-calendar", "alias": "work"},
        "drive": {"namespace": "mcp__google-drive", "alias": "work"},
    })
    se.add_send_account(data, "個人", "me@gmail.example", routes={
        "calendar": {"namespace": "mcp__google-calendar-personal", "alias": "personal"},
    })
    se.set_send_identity(data, "会社")
    return data


def test_ledger_default_empty():
    assert se.list_send_accounts(_fresh()) == []


def test_ledger_add_and_get_by_label_and_email():
    data = _with_ledger()
    assert len(se.list_send_accounts(data)) == 2
    assert se.get_send_account(data, "会社")["email"] == "sales@corp.example"
    # email lookup + case-insensitive
    assert se.get_send_account(data, "ME@GMAIL.EXAMPLE")["label"] == "個人"
    assert se.get_send_account(data, "missing") is None


def test_ledger_add_requires_label_and_email():
    data = _fresh()
    with pytest.raises(ValueError):
        se.add_send_account(data, "", "x@y.z")
    with pytest.raises(ValueError):
        se.add_send_account(data, "会社", "  ")


def test_ledger_add_is_idempotent_on_label():
    data = _fresh()
    se.add_send_account(data, "会社", "old@corp.example")
    se.add_send_account(data, "会社", "new@corp.example",
                        routes={"gmail": {"namespace": "mcp__gmail"}})
    assert len(se.list_send_accounts(data)) == 1
    assert se.get_send_account(data, "会社")["email"] == "new@corp.example"


def test_resolve_route_default_label():
    data = _with_ledger()
    r = se.resolve_route(data, "calendar")  # default = 会社
    assert r["namespace"] == "mcp__google-calendar" and r["alias"] == "work"
    assert r["label"] == "会社" and r["email"] == "sales@corp.example"


def test_resolve_route_explicit_label_switch():
    data = _with_ledger()
    r = se.resolve_route(data, "calendar", "個人")
    assert r["namespace"] == "mcp__google-calendar-personal"
    assert r["alias"] == "personal"


def test_resolve_route_gmail_has_no_alias():
    data = _with_ledger()
    r = se.resolve_route(data, "gmail", "会社")
    assert r["namespace"] == "mcp__gmail" and r["alias"] is None


def test_resolve_route_missing_service_returns_none():
    data = _with_ledger()
    assert se.resolve_route(data, "drive", "個人") is None  # 個人 has no drive route


def test_resolve_route_unknown_account_returns_none():
    data = _with_ledger()
    assert se.resolve_route(data, "gmail", "存在しない") is None


def test_resolve_route_rejects_unknown_service():
    with pytest.raises(ValueError):
        se.resolve_route(_with_ledger(), "slack")


def test_set_account_route_updates_in_place():
    data = _with_ledger()
    se.set_account_route(data, "個人", "gmail", "mcp__gmail_personal")
    r = se.resolve_route(data, "gmail", "個人")
    assert r["namespace"] == "mcp__gmail_personal"


def test_set_account_route_unknown_account_raises():
    with pytest.raises(ValueError):
        se.set_account_route(_fresh(), "会社", "gmail", "mcp__gmail")


def test_clean_routes_drops_unknown_service_and_empty_ns():
    data = _fresh()
    se.add_send_account(data, "会社", "a@b.c", routes={
        "gmail": {"namespace": "mcp__gmail"},
        "slack": {"namespace": "mcp__slack"},   # unknown service dropped
        "drive": {"namespace": ""},             # empty namespace dropped
    })
    routes = se.get_send_account(data, "会社")["routes"]
    assert set(routes.keys()) == {"gmail"}


def test_remove_send_account():
    data = _with_ledger()
    se.remove_send_account(data, "個人")
    assert se.get_send_account(data, "個人") is None
    with pytest.raises(ValueError):
        se.remove_send_account(data, "個人")


def test_check_send_from_ledger_match_by_email():
    data = _with_ledger()  # default label 会社 → sales@corp.example
    ok, msg = se.check_send_from(data, "sales@corp.example")
    assert ok is True and "会社" in msg


def test_check_send_from_ledger_label_switch_match():
    data = _with_ledger()
    ok, _ = se.check_send_from(data, "me@gmail.example", label="個人")
    assert ok is True


def test_check_send_from_ledger_mismatch_blocks():
    data = _with_ledger()
    # 個人's email against the 会社 default → mismatch
    ok, msg = se.check_send_from(data, "me@gmail.example")
    assert ok is False and "取り違え" in msg
