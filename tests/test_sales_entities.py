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


def test_seed_phases_carry_methodology():
    # ms-107 e-3375 + e-3581: the shipped base 4-phase seed carries goal /
    # activity_template / default_lead per phase. transition_signal was removed
    # (e-3581): judgement fires off the gate's anchored work-item, not a phase field.
    data = _fresh()
    m = se.opportunity_phase_methodology(data, "商談準備")
    assert m["goal"] and "transition_signal" not in m
    assert "初回面談を実施" in m["activity_template"] and m["default_lead"] == 7
    agree = se.opportunity_phase_methodology(data, "合意済み")
    assert agree["activity_template"] == ["契約書を送付", "締結"]
    kentou = se.opportunity_phase_methodology(data, "先方検討中")
    assert kentou["goal"] == "先方の実行合意を取る" and kentou["default_lead"] == 14


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
    se.contact_add(data, acc, "Alice", role="CTO", email="a@globex.com",
                   phone="03-1234-5678")
    contacts = se.find_account(data, acc)["contacts"]
    assert contacts == [{"name": "Alice", "role": "CTO", "email": "a@globex.com",
                         "phone": "03-1234-5678"}]


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
    # e-3580 fold: 入場で初期フェーズの前進ゲートが1つ open (履歴は gate列)
    g = se.current_gate(data, opp)
    assert g is not None and g["phase"] == "商談準備" and g["status"] == se.GATE_OPEN


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

def test_opportunity_phase_advance_history_on_gates():
    # e-3580 fold: the phase-change history lives on the done advance-gate列,
    # reached via the structured advance path (not a raw phase_set on the deal).
    data = _fresh()
    opp = se.opportunity_add(data, "Deal", created_at="T0")
    se.advance_transition(data, opp, at="T1")   # 商談準備 → 提案準備
    se.advance_transition(data, opp, at="T2")   # 提案準備 → 先方検討中
    o = se.find_opportunity(data, opp)
    assert o["phase"] == "先方検討中"
    assert "phase_history" not in o and "transition_date" not in o  # not on the deal
    assert [g["phase"] for g in se.phase_change_history(data, opp)] == ["商談準備", "提案準備"]
    assert se.current_gate(data, opp)["phase"] == "先方検討中"


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


def test_resolve_route_slack_namespace_only():
    data = _fresh()
    se.add_send_account(data, "会社", "sales@corp.example",
                        routes={"slack": {"namespace": "slack-ga"}})
    se.set_send_identity(data, "会社")
    r = se.resolve_route(data, "slack")
    assert r["namespace"] == "slack-ga" and r["alias"] is None


def test_resolve_route_rejects_unknown_service():
    with pytest.raises(ValueError):
        se.resolve_route(_with_ledger(), "teams")


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
        "teams": {"namespace": "mcp__teams"},   # unknown service dropped
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


# --- phase methodology (config schema) — ms-107 e-3371 ---------------------

def test_phase_methodology_defaults_for_bare_phase():
    # The ms-106 seed carries no methodology; accessors default gracefully.
    # e-3581: transition_signal is no longer a methodology field.
    m = se.phase_methodology({"name": "商談準備"})
    assert m["goal"] == ""
    assert m["activity_template"] == []
    assert "transition_signal" not in m
    assert m["on_fail"] is None
    assert m["default_lead"] is None


def test_phase_methodology_defaults_for_none():
    m = se.phase_methodology(None)
    assert m == {"goal": "", "activity_template": [], "on_fail": None,
                 "default_lead": None}


def test_phase_methodology_reads_configured_fields():
    pdef = {
        "name": "提案準備", "terminal": False, "allowed_terminals": ["成約", "失注"],
        "goal": "提案内容に合意をとる",
        "activity_template": ["提案書ドラフト", "見積提示"],
        "on_fail": {"terminals": ["失注"], "retry": True},
        "default_lead": 7,
    }
    m = se.phase_methodology(pdef)
    assert m["goal"] == "提案内容に合意をとる"
    assert m["activity_template"] == ["提案書ドラフト", "見積提示"]
    assert "transition_signal" not in m
    assert m["on_fail"] == {"terminals": ["失注"], "retry": True}
    assert m["default_lead"] == 7


def test_phase_default_lead_coerces_and_guards():
    assert se.phase_default_lead({"default_lead": "5"}) == 5
    assert se.phase_default_lead({"default_lead": "oops"}) is None
    assert se.phase_default_lead({}) is None


def test_phase_on_fail_ignores_non_dict():
    assert se.phase_on_fail({"on_fail": "nope"}) is None
    assert se.phase_on_fail({"on_fail": {"retry": True}}) == {"retry": True}


def test_opportunity_phase_methodology_by_name():
    data = _fresh()
    # Attach methodology to a seed phase (config is editable per company).
    prep = se._find_phase_def(data["opportunity_phases"], "商談準備")
    prep["goal"] = "面談を確定する"
    prep["default_lead"] = 3
    m = se.opportunity_phase_methodology(data, "商談準備")
    assert m["goal"] == "面談を確定する" and m["default_lead"] == 3
    # Unknown name → stable defaulted shape, no KeyError.
    assert se.opportunity_phase_methodology(data, "無い")["goal"] == ""


def test_opportunity_phase_is_terminal():
    data = _fresh()
    assert se.opportunity_phase_is_terminal(data, "成約") is True
    assert se.opportunity_phase_is_terminal(data, "商談準備") is False
    assert se.opportunity_phase_is_terminal(data, "未知") is False


# --- transition_date (遷移日) — ms-107 e-3371 ------------------------------

def test_opportunity_add_defaults_transition_date_empty():
    # e-3580 fold: the 遷移日 lives on the open gate, unset at creation.
    data = _fresh()
    opp = se.opportunity_add(data, "Deal", created_at="T0")
    assert se.get_transition_date(data, opp) == ""
    assert se.needs_transition_date(data, opp) is True


def test_opportunity_add_with_transition_date_seeds_gate():
    data = _fresh()
    opp = se.opportunity_add(data, "Deal", transition_date="2026-08-01", created_at="T0")
    assert se.get_transition_date(data, opp) == "2026-08-01"
    assert se.current_gate(data, opp)["transition_date"] == "2026-08-01"


def test_set_and_get_transition_date_is_append_only_on_gate():
    data = _fresh()
    opp = se.opportunity_add(data, "Deal", created_at="T0")
    se.set_transition_date(data, opp, "2026-08-01", note="面談日", at="T1")
    se.set_transition_date(data, opp, "2026-08-10", note="reschedule", at="T2")
    assert se.get_transition_date(data, opp) == "2026-08-10"
    g = se.current_gate(data, opp)
    dates = [h["transition_date"] for h in g["history"]
             if h.get("action") == "transition-date"]
    assert dates == ["2026-08-01", "2026-08-10"]


def test_set_transition_date_clear_is_logged_on_gate():
    data = _fresh()
    opp = se.opportunity_add(data, "Deal", transition_date="2026-08-01", created_at="T0")
    rec = se.set_transition_date(data, opp, "", note="cleared", at="T1")
    assert rec["transition_date"] == ""
    assert se.get_transition_date(data, opp) == ""
    # the clear is still recorded on the gate (append-only 証跡).
    last = se.current_gate(data, opp)["history"][-1]
    assert last["action"] == "transition-date" and last["note"] == "cleared"


def test_transition_date_unknown_opportunity():
    data = _fresh()
    with pytest.raises(ValueError):
        se.set_transition_date(data, "opp-9", "2026-08-01")
    with pytest.raises(ValueError):
        se.get_transition_date(data, "opp-9")


def test_transition_date_rejects_non_opportunity_target():
    data = _fresh()
    acc = se.account_add(data, "Globex")
    with pytest.raises(ValueError):
        se.set_transition_date(data, acc, "2026-08-01")


def test_needs_transition_date_true_when_unset_in_stage():
    data = _fresh()
    opp = se.opportunity_add(data, "Deal", created_at="T0")  # 商談準備, no date
    assert se.needs_transition_date(data, opp) is True


def test_needs_transition_date_false_once_set():
    data = _fresh()
    opp = se.opportunity_add(data, "Deal", created_at="T0")
    se.set_transition_date(data, opp, "2026-08-01", at="T1")
    assert se.needs_transition_date(data, opp) is False


def test_needs_transition_date_false_in_terminal_phase():
    data = _fresh()
    opp = se.opportunity_add(data, "Deal", created_at="T0")
    se.terminal_transition(data, opp, "不成立", at="T1")  # terminal — deal decided
    assert se.needs_transition_date(data, opp) is False


def test_needs_transition_date_false_for_unknown_or_account():
    data = _fresh()
    acc = se.account_add(data, "Globex")
    assert se.needs_transition_date(data, acc) is False
    assert se.needs_transition_date(data, "opp-99") is False


# --- transition judgement engine (3-way + overdue) — ms-107 e-3372 ---------

def _opp_in_stage(data, stage="商談準備", date=""):
    opp = se.opportunity_add(data, "Deal", phase=stage, transition_date=date, created_at="T0")
    return opp


def test_transition_status_unset_and_scheduled():
    data = _fresh()
    opp = _opp_in_stage(data)  # no date
    assert se.transition_status(data, opp, "2026-08-01") == se.TRANSITION_UNSET
    se.set_transition_date(data, opp, "2026-08-10", at="T1")
    assert se.transition_status(data, opp, "2026-08-01") == se.TRANSITION_SCHEDULED


def test_transition_status_due_and_overdue():
    data = _fresh()
    opp = _opp_in_stage(data, date="2026-08-01")
    assert se.transition_status(data, opp, "2026-08-01") == se.TRANSITION_DUE
    assert se.transition_status(data, opp, "2026-08-02") == se.TRANSITION_OVERDUE
    assert se.transition_status(data, opp, "2026-07-31") == se.TRANSITION_SCHEDULED


def test_transition_status_settled_in_terminal_regardless_of_date():
    data = _fresh()
    opp = _opp_in_stage(data, date="2026-08-01")
    se.phase_set(data, opp, "不成立", at="T1")
    # even with an old date, a terminal deal is settled (not overdue).
    assert se.transition_status(data, opp, "2027-01-01") == se.TRANSITION_SETTLED


def test_next_opportunity_phase_order_and_end():
    data = _fresh()
    assert se.next_opportunity_phase(data, "商談準備") == "提案準備"
    assert se.next_opportunity_phase(data, "先方検討中") == "合意済み"
    # last non-terminal → None (success from here is a terminal, not advance).
    assert se.next_opportunity_phase(data, "合意済み") is None
    assert se.next_opportunity_phase(data, "成約") is None
    assert se.next_opportunity_phase(data, "未知") is None


def test_advance_moves_phase_and_clears_date_when_none_given():
    data = _fresh()
    opp = _opp_in_stage(data, date="2026-08-01")
    res = se.advance_transition(data, opp, at="T1")
    assert res["phase"] == "提案準備"
    o = se.find_opportunity(data, opp)
    assert o["phase"] == "提案準備"
    # new phase = fresh gate with no date yet → prompts for a new one
    assert se.get_transition_date(data, opp) == ""
    assert se.needs_transition_date(data, opp) is True
    # the advanced-from gate settled with outcome=advance
    assert [g["outcome"] for g in se.phase_change_history(data, opp)] == ["advance"]


def test_advance_sets_next_date_when_given():
    data = _fresh()
    opp = _opp_in_stage(data, date="2026-08-01")
    se.advance_transition(data, opp, next_transition_date="2026-09-01", at="T1")
    assert se.get_transition_date(data, opp) == "2026-09-01"


def test_advance_from_last_stage_raises():
    data = _fresh()
    opp = _opp_in_stage(data, stage="合意済み", date="2026-08-01")
    with pytest.raises(ValueError):
        se.advance_transition(data, opp, at="T1")


def test_retry_keeps_phase_new_date_and_requires_date():
    data = _fresh()
    opp = _opp_in_stage(data, date="2026-08-01")
    se.retry_transition(data, opp, "2026-08-20", note="粘る", at="T1")
    o = se.find_opportunity(data, opp)
    assert o["phase"] == "商談準備"
    assert se.get_transition_date(data, opp) == "2026-08-20"
    # Option-3: retry settled the old gate (outcome=retry) + opened a fresh
    # same-phase gate; the phase did NOT change, so no phase-change entry.
    assert [g["outcome"] for g in se.gate_history(data, opp)] == ["retry"]
    assert se.phase_change_history(data, opp) == []
    with pytest.raises(ValueError):
        se.retry_transition(data, opp, "", at="T2")


def test_allowed_terminals_for_current_phase():
    data = _fresh()
    opp = _opp_in_stage(data, stage="提案準備")
    assert se.allowed_terminals_for(data, opp) == ["成約", "失注"]


def test_terminal_transition_settles_and_clears_date():
    data = _fresh()
    opp = _opp_in_stage(data, stage="提案準備", date="2026-08-01")
    se.terminal_transition(data, opp, "失注", at="T1")
    o = se.find_opportunity(data, opp)
    assert o["phase"] == "失注" and o["status"] == "lost"
    assert se.get_transition_date(data, opp) == ""      # no open gate after 決着
    assert se.current_gate(data, opp) is None
    assert se.transition_status(data, opp, "2026-08-02") == se.TRANSITION_SETTLED
    # the settled gate carries outcome=terminal (its phase was 提案準備)
    last = se.gate_history(data, opp)[-1]
    assert last["outcome"] == "terminal" and last["phase"] == "提案準備"


def test_terminal_transition_rejects_non_terminal_phase():
    data = _fresh()
    opp = _opp_in_stage(data, stage="提案準備")
    with pytest.raises(ValueError):
        se.terminal_transition(data, opp, "先方検討中")  # not terminal


def test_suggest_transition_date_from_default_lead():
    data = _fresh()
    prep = se._find_phase_def(data["opportunity_phases"], "提案準備")
    prep["default_lead"] = 7
    assert se.suggest_transition_date(data, "提案準備", "2026-08-01") == "2026-08-08"
    # no lead configured → None (clear the seed's lead to exercise this path)
    se._find_phase_def(data["opportunity_phases"], "商談準備")["default_lead"] = None
    assert se.suggest_transition_date(data, "商談準備", "2026-08-01") is None
    # bad base date → None (guarded)
    assert se.suggest_transition_date(data, "提案準備", "nope") is None


def test_opportunities_awaiting_judgement_scan_and_sort():
    data = _fresh()
    a = se.opportunity_add(data, "A", transition_date="2026-08-05", created_at="T0")  # future
    b = se.opportunity_add(data, "B", transition_date="2026-08-01", created_at="T0")  # overdue
    c = se.opportunity_add(data, "C", transition_date="2026-08-03", created_at="T0")  # due
    se.opportunity_add(data, "D", created_at="T0")                                    # unset
    rows = se.opportunities_awaiting_judgement(data, "2026-08-03")
    # only overdue(B) + due(C); sorted oldest-first (B before C); A/D excluded.
    assert [r["id"] for r in rows] == [b, c]
    assert rows[0]["transition_status"] == se.TRANSITION_OVERDUE
    assert rows[1]["transition_status"] == se.TRANSITION_DUE


def test_overdue_persists_until_judged():
    data = _fresh()
    opp = _opp_in_stage(data, date="2026-08-01")
    assert se.transition_status(data, opp, "2026-08-10") == se.TRANSITION_OVERDUE
    # retry (place a new future date) clears the overdue state.
    se.retry_transition(data, opp, "2026-08-20", at="T1")
    assert se.transition_status(data, opp, "2026-08-10") == se.TRANSITION_SCHEDULED


# --- phase-entry activity anchors (e-3270) ---------------------------------

def _with_anchor(data, phase="提案準備", anchors=None):
    pdef = se._find_phase_def(data["opportunity_phases"], phase)
    pdef["activity_template"] = anchors if anchors is not None else ["提案書を作成する"]


def test_advance_instantiates_next_phase_anchor_activities():
    data = _fresh()
    _with_anchor(data, "提案準備", ["提案書を作成する", "見積を用意する"])
    opp = se.opportunity_add(data, "Deal", phase="商談準備", transition_date="2026-08-01", created_at="T0")
    res = se.advance_transition(data, opp, at="T1")  # → 提案準備
    assert res["phase"] == "提案準備"
    acts = se.find_opportunity(data, opp)["activities"]
    assert [a["description"] for a in acts] == ["提案書を作成する", "見積を用意する"]
    assert all(a["source"] == "template-anchor" and a["status"] == "todo" for a in acts)
    assert res["activities"] == [a["id"] for a in acts]


def test_advance_into_phase_without_template_creates_nothing():
    data = _fresh()
    # explicitly clear the next phase's template to exercise the no-template path
    se._find_phase_def(data["opportunity_phases"], "提案準備")["activity_template"] = []
    opp = se.opportunity_add(data, "Deal", phase="商談準備", transition_date="2026-08-01", created_at="T0")
    se.advance_transition(data, opp, at="T1")
    assert se.find_opportunity(data, opp)["activities"] == []


def test_instantiate_phase_activities_is_idempotent_by_description():
    data = _fresh()
    _with_anchor(data, "商談準備", ["面談を打診する"])
    opp = se.opportunity_add(data, "Deal", phase="商談準備", created_at="T0")
    first = se.instantiate_phase_activities(data, opp, at="T1")
    second = se.instantiate_phase_activities(data, opp, at="T2")  # same anchor already present
    assert len(first) == 1 and second == []
    assert len(se.find_opportunity(data, opp)["activities"]) == 1


def test_instantiate_skips_anchor_matching_existing_but_readds_if_done():
    data = _fresh()
    _with_anchor(data, "商談準備", ["面談を打診する"])
    opp = se.opportunity_add(data, "Deal", phase="商談準備", created_at="T0")
    aid = se.instantiate_phase_activities(data, opp, at="T1")[0]
    # mark it done → the anchor is eligible to be re-created next entry.
    se.find_opportunity(data, opp)["activities"][0]["status"] = "done"
    again = se.instantiate_phase_activities(data, opp, at="T2")
    assert len(again) == 1 and again[0] != aid


def test_activity_add_records_source():
    data = _fresh()
    opp = se.opportunity_add(data, "Deal", created_at="T0")
    a = se.activity_add(data, opp, "手動タスク", source="ai", created_at="T1")
    act = se.find_opportunity(data, opp)["activities"][0]
    assert act["source"] == "ai"


# --- target-class temporal core + 締切精査 (e-3271) -------------------------

def test_temporal_status_pure_generic():
    assert se.temporal_status("", "2026-08-01") == se.TRANSITION_UNSET
    assert se.temporal_status("2026-08-01", "2026-08-01") == se.TRANSITION_DUE
    assert se.temporal_status("2026-07-30", "2026-08-01") == se.TRANSITION_OVERDUE
    assert se.temporal_status("2026-08-10", "2026-08-01") == se.TRANSITION_SCHEDULED
    # settled short-circuits regardless of date
    assert se.temporal_status("2020-01-01", "2026-08-01", settled=True) == se.TRANSITION_SETTLED


def test_scan_overdue_generic_over_arbitrary_items():
    # simulate a non-sales target class (e.g. milestone-like dicts) to prove the
    # scanner is entity-agnostic — this is the ms-109 second-consumer shape.
    items = [
        {"k": "A", "date": "2026-08-05", "done": False},   # future
        {"k": "B", "date": "2026-08-01", "done": False},   # overdue
        {"k": "C", "date": "2026-08-03", "done": False},   # due
        {"k": "D", "date": "", "done": False},             # no date
        {"k": "E", "date": "2026-07-01", "done": True},    # settled → excluded
    ]
    pairs = se.scan_overdue(items, lambda it: it["date"], lambda it: it["done"], "2026-08-03")
    assert [it["k"] for it, _ in pairs] == ["B", "C"]  # oldest first, settled/future/unset excluded
    assert pairs[0][1] == se.TRANSITION_OVERDUE and pairs[1][1] == se.TRANSITION_DUE


def test_transition_status_still_matches_after_refactor():
    data = _fresh()
    opp = se.opportunity_add(data, "Deal", phase="商談準備", transition_date="2026-08-01", created_at="T0")
    assert se.transition_status(data, opp, "2026-08-02") == se.TRANSITION_OVERDUE
    se.phase_set(data, opp, "不成立", at="T1")
    assert se.transition_status(data, opp, "2027-01-01") == se.TRANSITION_SETTLED


def test_awaiting_judgement_carries_ball_dimension():
    data = _fresh()
    a = se.opportunity_add(data, "A", transition_date="2026-08-01",
                           who_has_the_ball=se.BALL_SELF, created_at="T0")
    b = se.opportunity_add(data, "B", transition_date="2026-08-01",
                           who_has_the_ball=se.BALL_COUNTERPART, created_at="T0")
    rows = se.opportunities_awaiting_judgement(data, "2026-08-05")
    by_id = {r["id"]: r for r in rows}
    assert by_id[a]["who_has_the_ball"] == se.BALL_SELF
    assert by_id[b]["who_has_the_ball"] == se.BALL_COUNTERPART


# --- ms-106 fb3: 担当ユーザー (assignee) on Opportunity + Account ------------

def test_assignee_on_opportunity_and_account():
    data = _fresh()
    acc = se.account_add(data, "Globex", assignee="kida")
    opp = se.opportunity_add(data, "Deal", account_id=acc, assignee="sato")
    assert se.find_account(data, acc)["assignee"] == "kida"
    assert se.find_opportunity(data, opp)["assignee"] == "sato"


def test_set_assignee_dispatch_and_clear():
    data = _fresh()
    acc = se.account_add(data, "Globex")
    opp = se.opportunity_add(data, "Deal", account_id=acc)
    se.set_assignee(data, acc, "kida")
    se.set_assignee(data, opp, "sato")
    assert se.find_account(data, acc)["assignee"] == "kida"
    assert se.find_opportunity(data, opp)["assignee"] == "sato"
    se.set_assignee(data, opp, "")  # clear
    assert se.find_opportunity(data, opp)["assignee"] == ""


def test_set_assignee_unknown_target():
    data = _fresh()
    with pytest.raises(ValueError):
        se.set_assignee(data, "opp-99", "kida")
    with pytest.raises(ValueError):
        se.set_assignee(data, "xxx-1", "kida")


# --- ms-106 fb3: Nurturing (継続関係の業務) on Account -----------------------

def test_nurturing_add_nested_under_account():
    data = _fresh()
    acc = se.account_add(data, "Globex")
    nid = se.nurturing_add(data, acc, "年賀状を送る", deadline="2026-12-25",
                           created_at="T0")
    assert nid == "nrt-1"
    nurt = se.find_account(data, acc)["nurturings"]
    assert nurt == [{"id": "nrt-1", "description": "年賀状を送る",
                     "deadline": "2026-12-25", "status": "todo",
                     "who_has_the_ball": se.BALL_SELF, "source": "",
                     "created_at": "T0",
                     "created_in_phase": se.find_account(data, acc)["phase"]}]


def test_nurturing_add_requires_account_and_desc():
    data = _fresh()
    with pytest.raises(ValueError):
        se.nurturing_add(data, "acc-99", "x")
    acc = se.account_add(data, "Globex")
    with pytest.raises(ValueError):
        se.nurturing_add(data, acc, "   ")


def test_nurturing_ids_are_account_global():
    data = _fresh()
    a1 = se.account_add(data, "A")
    a2 = se.account_add(data, "B")
    assert se.nurturing_add(data, a1, "x") == "nrt-1"
    assert se.nurturing_add(data, a2, "y") == "nrt-2"


# --- ms-106 fb3 / e-3350: 顧客フェーズ ← 商談フェーズ の連動 -----------------

def test_derive_account_phase_lead_when_no_or_prep_only_opps():
    data = _fresh()
    acc = se.account_add(data, "Globex")
    assert se.derive_account_phase(data, acc) == "リード"  # no opps
    se.opportunity_add(data, "Deal", account_id=acc)  # 商談準備 entry
    assert se.derive_account_phase(data, acc) == "リード"


def test_account_auto_advances_up_on_opportunity_progress():
    data = _fresh()
    acc = se.account_add(data, "Globex")
    opp = se.opportunity_add(data, "Deal", account_id=acc, created_at="T0")
    assert se.find_account(data, acc)["phase"] == "リード"
    se.phase_set(data, opp, "提案準備", at="T1")
    assert se.find_account(data, acc)["phase"] == "未成約顧客"
    se.phase_set(data, opp, "成約", at="T2")
    assert se.find_account(data, acc)["phase"] == "成約顧客"


def test_account_phase_never_auto_downgrades():
    data = _fresh()
    acc = se.account_add(data, "Globex")
    won = se.opportunity_add(data, "Won deal", account_id=acc, created_at="T0")
    se.phase_set(data, won, "成約", at="T1")
    assert se.find_account(data, acc)["phase"] == "成約顧客"
    # a brand-new lead-stage deal must not pull the customer back to リード
    fresh = se.opportunity_add(data, "New deal", account_id=acc, created_at="T2")
    se.phase_set(data, fresh, "商談準備", at="T3")
    assert se.find_account(data, acc)["phase"] == "成約顧客"


def test_lost_deal_marks_account_as_prospect():
    data = _fresh()
    acc = se.account_add(data, "Globex")
    opp = se.opportunity_add(data, "Deal", account_id=acc, created_at="T0")
    se.phase_set(data, opp, "提案準備", at="T1")
    se.phase_set(data, opp, "失注", at="T2")
    assert se.find_account(data, acc)["phase"] == "未成約顧客"


def test_account_auto_advance_records_history_note():
    data = _fresh()
    acc = se.account_add(data, "Globex")
    opp = se.opportunity_add(data, "Deal", account_id=acc, created_at="T0")
    se.phase_set(data, opp, "提案準備", at="T1")
    hist = se.find_account(data, acc)["phase_history"]
    assert hist[-1]["phase"] == "未成約顧客"
    assert "自動昇格" in hist[-1]["note"]


# --- ms-106 fb4: 商談金額 / フェーズ成約率 / 見込み売上 / メンバー目標 --------

def test_seed_phase_probabilities():
    data = _fresh()
    probs = {p["name"]: p.get("probability") for p in data["opportunity_phases"]}
    assert probs["商談準備"] == 10 and probs["提案準備"] == 20
    assert probs["先方検討中"] == 40 and probs["合意済み"] == 80
    assert probs["成約"] == 100 and probs["失注"] == 0 and probs["不成立"] == 0


def test_set_opportunity_amount():
    data = _fresh()
    opp = se.opportunity_add(data, "Deal")
    se.set_opportunity_amount(data, opp, 1500000)
    assert se.find_opportunity(data, opp)["goal_amount"] == 1500000
    with pytest.raises(ValueError):
        se.set_opportunity_amount(data, "opp-99", 1)


def test_set_phase_probability():
    data = _fresh()
    se.set_phase_probability(data, "商談準備", 15)
    assert se._find_phase_def(data["opportunity_phases"], "商談準備")["probability"] == 15
    with pytest.raises(ValueError):
        se.set_phase_probability(data, "存在しない", 10)


def test_weighted_pipeline_total_and_per_assignee():
    data = _fresh()
    a = se.account_add(data, "A")
    o1 = se.opportunity_add(data, "D1", account_id=a, assignee="kida")  # 商談準備 10%
    se.set_opportunity_amount(data, o1, 1000000)
    o2 = se.opportunity_add(data, "D2", account_id=a, assignee="kida")
    se.phase_set(data, o2, "提案準備")  # 20%
    se.set_opportunity_amount(data, o2, 2000000)
    o3 = se.opportunity_add(data, "D3", account_id=a, assignee="sato")
    se.phase_set(data, o3, "合意済み")  # 80%
    se.set_opportunity_amount(data, o3, 5000000)
    assert se.weighted_pipeline(data) == 4500000.0
    assert se.weighted_pipeline(data, assignee="kida") == 500000.0
    assert se.weighted_pipeline(data, assignee="sato") == 4000000.0


def test_weighted_pipeline_excludes_terminal_and_amountless():
    data = _fresh()
    o = se.opportunity_add(data, "Won", assignee="kida")
    se.set_opportunity_amount(data, o, 9000000)
    se.phase_set(data, o, "成約")  # terminal → excluded
    noamt = se.opportunity_add(data, "NoAmount", assignee="kida")  # 商談準備, no amount
    se.phase_set(data, noamt, "提案準備")
    assert se.weighted_pipeline(data) == 0.0


def test_sales_targets_sparse_set_get_clear():
    data = _fresh()
    assert se.get_sales_target(data, "kida") is None  # unset member
    se.set_sales_target(data, "kida", 3000000)
    assert se.get_sales_target(data, "kida") == 3000000
    assert se.get_sales_target(data, "sato") is None  # member added later, no entry
    se.set_sales_target(data, "kida", None)  # clear
    assert se.get_sales_target(data, "kida") is None
    with pytest.raises(ValueError):
        se.set_sales_target(data, "  ", 1)


# --- Communication (証跡・事後記録型 = 営業の Commit, e-3432) ---------------

def test_communication_add_under_opportunity():
    data = _fresh()
    opp = se.opportunity_add(data, "Deal")
    cid = se.communication_add(data, opp, "日程を打診", direction="outbound",
                               channel="email",
                               source={"ref": "<m1@x>", "url": "https://x/1"},
                               occurred_at="2026-07-15T10:00:00+09:00")
    assert cid == "comm-1"
    c = se.find_opportunity(data, opp)["communications"][0]
    assert c["direction"] == "outbound" and c["channel"] == "email"
    assert c["summary"] == "日程を打診"
    assert c["source"] == {"ref": "<m1@x>", "url": "https://x/1"}
    assert c["occurred_at"] == "2026-07-15T10:00:00+09:00"


def test_communication_add_under_account():
    data = _fresh()
    acc = se.account_add(data, "Acme")
    cid = se.communication_add(data, acc, "御礼メール受領", direction="inbound")
    assert cid == "comm-1"
    c = se.find_account(data, acc)["communications"][0]
    assert c["channel"] == "other"  # default when unspecified
    assert c["direction"] == "inbound"


def test_communication_ids_global_across_targets():
    data = _fresh()
    acc = se.account_add(data, "Acme")
    opp = se.opportunity_add(data, "Deal", account_id=acc)
    assert se.communication_add(data, opp, "a", direction="outbound") == "comm-1"
    assert se.communication_add(data, acc, "b", direction="inbound") == "comm-2"
    assert se.communication_add(data, opp, "c", direction="inbound") == "comm-3"


def test_communication_unknown_target():
    data = _fresh()
    with pytest.raises(ValueError):
        se.communication_add(data, "opp-9", "x", direction="inbound")
    with pytest.raises(ValueError):
        se.communication_add(data, "acc-9", "x", direction="inbound")
    with pytest.raises(ValueError):
        se.communication_add(data, "mlst-1", "x", direction="inbound")  # wrong prefix


def test_communication_validates_direction_but_channel_is_free_text():
    data = _fresh()
    opp = se.opportunity_add(data, "Deal")
    with pytest.raises(ValueError):
        se.communication_add(data, opp, "x", direction="sideways")
    with pytest.raises(ValueError):
        se.communication_add(data, opp, "  ", direction="inbound")  # blank summary
    # e-3454: channel is free-text — off-pipeline channels are accepted and
    # normalized (strip + lowercase); empty falls back to "other".
    c1 = se.communication_add(data, opp, "Messengerで日程調整", direction="inbound",
                              channel="Facebook Messenger")
    c2 = se.communication_add(data, opp, "電話で確認", direction="inbound", channel="  LINE  ")
    c3 = se.communication_add(data, opp, "媒体不明", direction="inbound", channel="")
    comms = {c["id"]: c for c in se.find_opportunity(data, opp)["communications"]}
    assert comms[c1]["channel"] == "facebook messenger"
    assert comms[c2]["channel"] == "line"
    assert comms[c3]["channel"] == "other"


def test_communications_of_orders_by_occurred_then_insertion():
    data = _fresh()
    opp = se.opportunity_add(data, "Deal")
    se.communication_add(data, opp, "third", direction="inbound", occurred_at="2026-07-15T14:00")
    se.communication_add(data, opp, "first", direction="outbound", occurred_at="2026-07-15T09:00")
    se.communication_add(data, opp, "notime", direction="inbound")  # no timestamp → keeps insert order (last)
    ordered = se.communications_of(se.find_opportunity(data, opp))
    assert [c["summary"] for c in ordered] == ["first", "third", "notime"]


def test_derive_ball_from_latest_communication():
    data = _fresh()
    opp = se.opportunity_add(data, "Deal")
    assert se.derive_ball(se.find_opportunity(data, opp)) is None  # no comms yet
    se.communication_add(data, opp, "sent", direction="outbound", occurred_at="2026-07-15T09:00")
    assert se.derive_ball(se.find_opportunity(data, opp)) == se.BALL_COUNTERPART
    se.communication_add(data, opp, "reply", direction="inbound", occurred_at="2026-07-15T14:00")
    assert se.derive_ball(se.find_opportunity(data, opp)) == se.BALL_SELF


def test_opportunity_delete_removes_communications():
    data = _fresh()
    opp = se.opportunity_add(data, "Deal")
    se.communication_add(data, opp, "x", direction="inbound")
    se.opportunity_delete(data, opp)
    assert se.find_opportunity(data, opp) is None


# --- Meeting (面談・運用状態型 + 識別 ID handshake, e-3433) -----------------

def test_meeting_schedule_sets_id_and_status():
    data = _fresh()
    opp = se.opportunity_add(data, "Deal")
    mid = se.meeting_schedule(data, opp, "2026-07-20T14:00:00+09:00",
                              end_at="2026-07-20T15:00:00+09:00", location="オンライン",
                              calendar_event_id="ev-abc", at="T0")
    assert mid == "mtg-1"
    o, m = se.find_meeting(data, mid)
    assert o["id"] == opp and m["status"] == "scheduled"
    assert m["calendar_event_id"] == "ev-abc"
    assert m["history"][0]["action"] == "scheduled"


def test_meeting_schedule_set_transition_updates_transition_date():
    data = _fresh()
    opp = se.opportunity_add(data, "Deal")
    se.meeting_schedule(data, opp, "2026-07-20T14:00:00+09:00",
                        set_transition=True, at="T0")
    # 遷移日 is the date portion of the meeting time
    assert se.get_transition_date(data, opp) == "2026-07-20"


def test_meeting_ids_global_across_opportunities():
    data = _fresh()
    o1 = se.opportunity_add(data, "D1")
    o2 = se.opportunity_add(data, "D2")
    assert se.meeting_schedule(data, o1, "2026-07-20T10:00") == "mtg-1"
    assert se.meeting_schedule(data, o2, "2026-07-21T10:00") == "mtg-2"


def test_meeting_schedule_requires_time_and_known_opp():
    data = _fresh()
    opp = se.opportunity_add(data, "Deal")
    with pytest.raises(ValueError):
        se.meeting_schedule(data, opp, "  ")
    with pytest.raises(ValueError):
        se.meeting_schedule(data, "opp-9", "2026-07-20T10:00")


def test_meeting_reschedule_moves_time_and_transition():
    data = _fresh()
    opp = se.opportunity_add(data, "Deal")
    mid = se.meeting_schedule(data, opp, "2026-07-20T14:00", set_transition=True, at="T0")
    se.meeting_reschedule(data, mid, "2026-07-25T16:00", set_transition=True, at="T1")
    _, m = se.find_meeting(data, mid)
    assert m["scheduled_at"] == "2026-07-25T16:00"
    assert m["history"][-1]["action"] == "rescheduled"
    assert se.get_transition_date(data, opp) == "2026-07-25"


def test_meeting_mark_ended_is_idempotent():
    data = _fresh()
    opp = se.opportunity_add(data, "Deal")
    mid = se.meeting_schedule(data, opp, "2026-07-20T14:00", at="T0")
    se.meeting_mark_ended(data, mid, at="T1")
    _, m = se.find_meeting(data, mid)
    assert m["status"] == "ended"
    hist_len = len(m["history"])
    se.meeting_mark_ended(data, mid, at="T2")  # second call is a no-op
    _, m2 = se.find_meeting(data, mid)
    assert m2["status"] == "ended" and len(m2["history"]) == hist_len


def test_meeting_calendar_tag_roundtrips():
    tag = se.meeting_calendar_tag("mtg-7")
    assert "mtg-7" in tag
    desc = f"面談です。\n\n{tag}\nよろしくお願いします。"
    assert se.parse_meeting_tag(desc) == "mtg-7"
    assert se.parse_meeting_tag("no tag here") is None
    assert se.parse_meeting_tag("beacon-meeting-id: garbage") is None


def test_opportunity_meetings_ordered_by_scheduled_time():
    data = _fresh()
    opp = se.opportunity_add(data, "Deal")
    se.meeting_schedule(data, opp, "2026-07-25T10:00")
    se.meeting_schedule(data, opp, "2026-07-20T10:00")
    ordered = se.opportunity_meetings(se.find_opportunity(data, opp))
    assert [m["scheduled_at"] for m in ordered] == ["2026-07-20T10:00", "2026-07-25T10:00"]


# --- Communication ↔ work-item linkage (act-/nrt-, e-3451) ------------------

def test_communication_links_to_activity_stored_under_opportunity():
    data = _fresh()
    opp = se.opportunity_add(data, "Deal")
    act = se.activity_add(data, opp, "提案書を送る")
    cid = se.communication_add(data, act, "提案書を送付した", direction="outbound", channel="email")
    # e-3503 — nested under the activity it fulfilled (commit-under-task), not at
    # the opportunity level. communications_of(opp) still gathers it.
    o = se.find_opportunity(data, opp)
    assert o.get("communications", []) == []
    _, a = se.find_activity(data, act)
    assert [c["id"] for c in a["communications"]] == [cid]
    assert a["communications"][0]["linked_id"] == act
    assert [c["id"] for c in se.communications_of(o)] == [cid]


def test_communication_links_to_nurturing_stored_under_account():
    data = _fresh()
    acc = se.account_add(data, "Acme")
    nrt = se.nurturing_add(data, acc, "年始の挨拶を送る")
    cid = se.communication_add(data, nrt, "年賀メールを送った", direction="outbound", channel="email")
    a = se.find_account(data, acc)
    assert a.get("communications", []) == []  # e-3503 — nested under nurturing
    _, n = se.find_nurturing(data, nrt)
    assert n["communications"][0]["id"] == cid
    assert n["communications"][0]["linked_id"] == nrt
    assert [c["id"] for c in se.communications_of(a)] == [cid]


def test_communication_target_grain_has_empty_linked_id():
    data = _fresh()
    acc = se.account_add(data, "Acme")
    opp = se.opportunity_add(data, "Deal", account_id=acc)
    c_acc = se.communication_add(data, acc, "a", direction="inbound")
    c_opp = se.communication_add(data, opp, "b", direction="inbound")
    assert se.find_account(data, acc)["communications"][0]["linked_id"] == ""
    assert se.find_opportunity(data, opp)["communications"][0]["linked_id"] == ""


def test_communications_of_filters_by_linked_id():
    data = _fresh()
    acc = se.account_add(data, "Acme")
    n1 = se.nurturing_add(data, acc, "n1")
    n2 = se.nurturing_add(data, acc, "n2")
    se.communication_add(data, n1, "for n1", direction="outbound")
    se.communication_add(data, acc, "account-level", direction="inbound")
    se.communication_add(data, n2, "for n2", direction="outbound")
    a = se.find_account(data, acc)
    assert len(se.communications_of(a)) == 3               # all
    only_n1 = se.communications_of(a, linked_id=n1)
    assert [c["summary"] for c in only_n1] == ["for n1"]


def test_resolve_communication_target_all_prefixes():
    data = _fresh()
    acc = se.account_add(data, "Acme")
    opp = se.opportunity_add(data, "Deal", account_id=acc)
    act = se.activity_add(data, opp, "call")
    nrt = se.nurturing_add(data, acc, "greet")
    assert se.resolve_communication_target(data, opp) == (se.find_opportunity(data, opp), "")
    assert se.resolve_communication_target(data, acc) == (se.find_account(data, acc), "")
    c_opp, l_act = se.resolve_communication_target(data, act)
    assert c_opp["id"] == opp and l_act == act
    c_acc, l_nrt = se.resolve_communication_target(data, nrt)
    assert c_acc["id"] == acc and l_nrt == nrt
    assert se.resolve_communication_target(data, "nrt-99") == (None, None)
    assert se.resolve_communication_target(data, "mlst-1") == (None, None)


def test_communication_unknown_work_item_raises():
    data = _fresh()
    with pytest.raises(ValueError):
        se.communication_add(data, "act-99", "x", direction="inbound")
    with pytest.raises(ValueError):
        se.communication_add(data, "nrt-99", "x", direction="inbound")


def test_derive_ball_includes_work_item_linked_comms():
    data = _fresh()
    acc = se.account_add(data, "Acme")
    nrt = se.nurturing_add(data, acc, "greet")
    se.communication_add(data, nrt, "sent greeting", direction="outbound", occurred_at="2026-07-15T09:00")
    # account-level ball reflects the nurturing-linked communication too
    assert se.derive_ball(se.find_account(data, acc)) == se.BALL_COUNTERPART


# --- meeting end-detection core (C, e-3434) ---------------------------------

def test_scan_ended_meetings_by_end_at_timezone_aware():
    data = _fresh()
    opp = se.opportunity_add(data, "Deal")
    # ended: end_at in the past (JST) vs now (UTC) — same-instant compare, not string
    m_past = se.meeting_schedule(data, opp, "2026-07-20T14:00:00+09:00",
                                 end_at="2026-07-20T15:00:00+09:00")
    # future: end_at after now
    se.meeting_schedule(data, opp, "2026-07-25T14:00:00+09:00",
                        end_at="2026-07-25T15:00:00+09:00")
    now = "2026-07-20T09:00:00+00:00"  # = 18:00 JST on the 20th → first meeting ended
    ended = se.scan_ended_meetings(data, now)
    assert [m["id"] for _, m in ended] == [m_past]


def test_scan_ended_meetings_falls_back_to_scheduled_at_when_no_end():
    data = _fresh()
    opp = se.opportunity_add(data, "Deal")
    m = se.meeting_schedule(data, opp, "2026-07-20T10:00:00+00:00")  # no end_at
    assert se.scan_ended_meetings(data, "2026-07-20T09:00:00+00:00") == []   # not yet
    ended = se.scan_ended_meetings(data, "2026-07-20T11:00:00+00:00")        # past
    assert [mm["id"] for _, mm in ended] == [m]


def test_scan_ended_meetings_idempotent_by_status():
    data = _fresh()
    opp = se.opportunity_add(data, "Deal")
    m = se.meeting_schedule(data, opp, "2026-07-20T10:00:00+00:00")
    now = "2026-07-20T12:00:00+00:00"
    assert len(se.scan_ended_meetings(data, now)) == 1
    se.meeting_mark_ended(data, m)  # C marks it → drops out (no double fire)
    assert se.scan_ended_meetings(data, now) == []
    # cancelled meetings never surface either
    m2 = se.meeting_schedule(data, opp, "2026-07-20T10:00:00+00:00")
    se.meeting_cancel(data, m2)
    assert se.scan_ended_meetings(data, now) == []


def test_scan_ended_meetings_ordered_earliest_end_first():
    data = _fresh()
    o1 = se.opportunity_add(data, "D1")
    o2 = se.opportunity_add(data, "D2")
    late = se.meeting_schedule(data, o1, "2026-07-20T10:00:00+00:00", end_at="2026-07-20T11:00:00+00:00")
    early = se.meeting_schedule(data, o2, "2026-07-20T08:00:00+00:00", end_at="2026-07-20T09:00:00+00:00")
    ended = se.scan_ended_meetings(data, "2026-07-20T12:00:00+00:00")
    assert [m["id"] for _, m in ended] == [early, late]


def test_scan_ended_meetings_bad_now_returns_empty():
    data = _fresh()
    opp = se.opportunity_add(data, "Deal")
    se.meeting_schedule(data, opp, "2026-07-20T10:00:00+00:00")
    assert se.scan_ended_meetings(data, "") == []
    assert se.scan_ended_meetings(data, "not-a-date") == []


# --- Watch (返信待ち見張りフラグ, E e-3437) ---------------------------------

def test_set_and_clear_watch_on_activity():
    data = _fresh()
    opp = se.opportunity_add(data, "Deal")
    act = se.activity_add(data, opp, "日程を打診")
    w = se.set_watch(data, act, channel="Email", thread_ref="<thr-1>", cadence_minutes=60, at="T0")
    assert w["enabled"] is True and w["channel"] == "email" and w["thread_ref"] == "<thr-1>"
    a = se.find_opportunity(data, opp)["activities"][0]
    assert a["watch"]["enabled"] is True
    se.clear_watch(data, act, at="T1")
    assert a["watch"]["enabled"] is False and a["watch"]["cleared_at"] == "T1"


def test_set_watch_on_nurturing_and_unknown():
    data = _fresh()
    acc = se.account_add(data, "Acme")
    nrt = se.nurturing_add(data, acc, "年始挨拶")
    se.set_watch(data, nrt, channel="slack")
    assert se.find_account(data, acc)["nurturings"][0]["watch"]["enabled"] is True
    with pytest.raises(ValueError):
        se.set_watch(data, "act-99", channel="email")
    with pytest.raises(ValueError):
        se.set_watch(data, nrt, channel="")  # channel required


def test_watched_work_items_awaiting_reply_filter():
    data = _fresh()
    opp = se.opportunity_add(data, "Deal")
    act = se.activity_add(data, opp, "打診")
    se.set_watch(data, act, channel="email")
    # no communications yet → ball None → not "awaiting reply from counterpart"
    assert se.watched_work_items(data) == [(se.find_opportunity(data, opp),
                                            se.find_opportunity(data, opp)["activities"][0])] \
        or len(se.watched_work_items(data)) == 1
    assert se.watched_work_items(data, awaiting_reply_only=True) == []
    # we send (outbound) → ball=counterpart → now awaiting reply
    se.communication_add(data, act, "打診メール送信", direction="outbound", occurred_at="2026-07-15T09:00")
    awaiting = se.watched_work_items(data, awaiting_reply_only=True)
    assert [wi["id"] for _, wi in awaiting] == [act]
    # reply arrives (inbound) → ball flips to self → drops out of awaiting
    se.communication_add(data, act, "返信あり", direction="inbound", occurred_at="2026-07-15T14:00")
    assert se.watched_work_items(data, awaiting_reply_only=True) == []


def test_watched_work_items_excludes_disabled():
    data = _fresh()
    acc = se.account_add(data, "Acme")
    nrt = se.nurturing_add(data, acc, "挨拶")
    se.set_watch(data, nrt, channel="email")
    se.clear_watch(data, nrt)
    assert se.watched_work_items(data) == []


def test_activity_set_status_marks_done():
    # ms-106 e-3505 — a send marks the fulfilled plan done, not a new todo.
    data = _fresh()
    opp = se.opportunity_add(data, "Deal")
    aid = se.activity_add(data, opp, "初回面談を打診")
    act = se.activity_set_status(data, aid, "done", at="T1")
    assert act["status"] == "done" and act["done_at"] == "T1"


def test_activity_set_status_validates():
    data = _fresh()
    opp = se.opportunity_add(data, "Deal")
    aid = se.activity_add(data, opp, "x")
    import pytest as _pt
    with _pt.raises(ValueError):
        se.activity_set_status(data, aid, "bogus")
    with _pt.raises(ValueError):
        se.activity_set_status(data, "act-999", "done")


# --- activity_cancel: sales-side cancel vocabulary (ms-109 e-3558, SPEC AC3) -

def test_activity_cancel_soft_cancels_via_base(monkeypatch):
    monkeypatch.setenv("BEACON_CLAUDE_CODE", "1")
    data = _fresh()
    oid = se.opportunity_add(data, "Deal", created_at="T0")
    aid = se.activity_add(data, oid, "初回連絡", created_at="T0")
    out = se.activity_cancel(data, aid, reason="誤起票")
    assert out["status"] == "cancelled"
    assert out["meta"]["cancelled_by"] == "claude"
    assert out["meta"]["cancel_reason"] == "誤起票"
    # Append-only: the activity is not removed from the opportunity.
    _, act = se.find_activity(data, aid)
    assert act is not None and act["status"] == "cancelled"
    # Its own data survives (immutability).
    assert act["description"] == "初回連絡"


def test_activity_cancel_unknown_id_raises():
    data = _fresh()
    with pytest.raises(ValueError):
        se.activity_cancel(data, "act-999")


# --- e-3537: 誤起票の訂正 (communication cancel / retarget + reader filtering) --

def test_communication_cancel_soft_and_excluded_from_ball(monkeypatch):
    monkeypatch.setenv("BEACON_CLAUDE_CODE", "1")
    data = _fresh()
    oid = se.opportunity_add(data, "Deal", created_at="T0")
    cid = se.communication_add(data, oid, "送ったつもりのメール",
                               direction=se.COMM_OUTBOUND, created_at="T1")
    out = se.communication_cancel(data, cid, reason="そもそも送ってない")
    assert out["status"] == "cancelled"
    assert out["meta"]["cancel_reason"] == "そもそも送ってない"
    opp = se.find_opportunity(data, oid)
    # 表示には残る (include_cancelled 既定 True)
    assert any(c["id"] == cid for c in se.communications_of(opp))
    # 判定 (ball) からは除外される — 有効な証跡が無いので None
    assert se.communications_of(opp, include_cancelled=False) == []
    assert se.derive_ball(opp) is None


def test_ball_uses_latest_non_cancelled(monkeypatch):
    monkeypatch.setenv("BEACON_CLAUDE_CODE", "1")
    data = _fresh()
    oid = se.opportunity_add(data, "Deal", created_at="T0")
    se.communication_add(data, oid, "こちらから連絡",
                         direction=se.COMM_OUTBOUND, occurred_at="2026-07-01")
    c2 = se.communication_add(data, oid, "返信(実は誤記録)",
                              direction=se.COMM_INBOUND, occurred_at="2026-07-02")
    opp = se.find_opportunity(data, oid)
    assert se.derive_ball(opp) == se.BALL_SELF  # 最新=inbound → 自分の番
    se.communication_cancel(data, c2, reason="誤記録")
    # 取消後は最新の有効証跡=outbound → 相手の番
    assert se.derive_ball(opp) == se.BALL_COUNTERPART


def test_communication_cancel_unknown_raises():
    data = _fresh()
    with pytest.raises(ValueError):
        se.communication_cancel(data, "comm-999")


def test_communication_retarget_moves_record_fact_intact():
    data = _fresh()
    oid = se.opportunity_add(data, "Deal")
    a1 = se.activity_add(data, oid, "初回連絡")
    a2 = se.activity_add(data, oid, "提案")
    cid = se.communication_add(data, a1, "議事録", direction=se.COMM_INBOUND,
                               channel="meeting", occurred_at="2026-07-03")
    _, _, before = se.find_communication(data, cid)
    assert before["linked_id"] == a1
    se.communication_retarget(data, cid, a2)
    _, _, after = se.find_communication(data, cid)
    # 綴じ場所 (linked_id) だけ変わり、事実フィールドは不変
    assert after["linked_id"] == a2
    assert after["summary"] == "議事録"
    assert after["direction"] == se.COMM_INBOUND
    assert after["occurred_at"] == "2026-07-03"
    # 重複しない (1レコードが移動しただけ)
    opp = se.find_opportunity(data, oid)
    assert len([c for c in se._gather_communications(opp) if c["id"] == cid]) == 1
    act1 = next(a for a in opp["activities"] if a["id"] == a1)
    act2 = next(a for a in opp["activities"] if a["id"] == a2)
    assert not any(c["id"] == cid for c in act1.get("communications", []))
    assert any(c["id"] == cid for c in act2.get("communications", []))


def test_communication_retarget_to_target_grain():
    data = _fresh()
    oid = se.opportunity_add(data, "Deal")
    a1 = se.activity_add(data, oid, "初回連絡")
    cid = se.communication_add(data, a1, "メール", direction=se.COMM_OUTBOUND)
    se.communication_retarget(data, cid, oid)  # act- → opp- (target grain)
    _, _, comm = se.find_communication(data, cid)
    assert comm["linked_id"] == ""
    opp = se.find_opportunity(data, oid)
    assert any(c["id"] == cid for c in opp.get("communications", []))


def test_communication_retarget_unknown_target_raises():
    data = _fresh()
    oid = se.opportunity_add(data, "Deal")
    cid = se.communication_add(data, oid, "x", direction=se.COMM_OUTBOUND)
    with pytest.raises(ValueError):
        se.communication_retarget(data, cid, "opp-999")


def test_watched_work_items_skips_cancelled():
    data = _fresh()
    oid = se.opportunity_add(data, "Deal")
    a1 = se.activity_add(data, oid, "日程打診")
    se.set_watch(data, a1, channel="email")
    assert any(a["id"] == a1 for _, a in se.watched_work_items(data))
    se.activity_cancel(data, a1, reason="誤り")
    assert not any(a["id"] == a1 for _, a in se.watched_work_items(data))


# --- e-3555: created_in_phase (set-once phase 帰属) ------------------------

def test_activity_created_in_phase_defaults_to_current():
    data = _fresh()
    oid = se.opportunity_add(data, "Deal")
    cur = se.find_opportunity(data, oid)["phase"]
    aid = se.activity_add(data, oid, "初回連絡")
    _, act = se.find_activity(data, aid)
    assert act["created_in_phase"] == cur


def test_activity_created_in_phase_explicit_seed_wins():
    data = _fresh()
    oid = se.opportunity_add(data, "Deal")
    aid = se.activity_add(data, oid, "先出し", created_in_phase="提案準備")
    _, act = se.find_activity(data, aid)
    assert act["created_in_phase"] == "提案準備"


def test_activity_created_in_phase_immutable_across_phase_change():
    data = _fresh()
    oid = se.opportunity_add(data, "Deal")
    opp = se.find_opportunity(data, oid)
    first = opp["phase"]
    aid = se.activity_add(data, oid, "x")
    opp["phase"] = "別フェーズ"  # 商談が進んでも
    _, act = se.find_activity(data, aid)
    assert act["created_in_phase"] == first  # 活動の生誕フェーズは不変


def test_communication_created_in_phase_from_container():
    data = _fresh()
    oid = se.opportunity_add(data, "Deal")
    cur = se.find_opportunity(data, oid)["phase"]
    cid = se.communication_add(data, oid, "メール", direction=se.COMM_OUTBOUND)
    _, _, comm = se.find_communication(data, cid)
    assert comm["created_in_phase"] == cur


def test_communication_created_in_phase_survives_retarget():
    data = _fresh()
    oid = se.opportunity_add(data, "Deal")
    opp = se.find_opportunity(data, oid)
    cur = opp["phase"]
    a1 = se.activity_add(data, oid, "初回")
    cid = se.communication_add(data, a1, "議事録", direction=se.COMM_INBOUND)
    opp["phase"] = "提案準備"
    a2 = se.activity_add(data, oid, "提案")
    se.communication_retarget(data, cid, a2)  # 綴じ替えても
    _, _, comm = se.find_communication(data, cid)
    assert comm["created_in_phase"] == cur  # set-once、不変


def test_nurturing_created_in_phase_from_account():
    data = _fresh()
    acc = se.account_add(data, "Globex")
    cur = se.find_account(data, acc)["phase"]
    nid = se.nurturing_add(data, acc, "定期フォロー")
    _, nrt = se.find_nurturing(data, nid)
    assert nrt["created_in_phase"] == cur


# --- e-3548: meeting-type anchor seeds a 予定未定 Meeting (dup 根絶) ----------

def _opp_phase_def(data, name):
    return se._find_phase_def(se.opportunity_phases(data), name)


def test_phase_activity_template_stays_string_list_backcompat():
    data = _fresh()
    tpl = se.phase_activity_template(_opp_phase_def(data, "商談準備"))
    assert tpl == ["初回面談を打診", "初回面談を実施", "提案の方向性を確定"]


def test_phase_activity_anchors_marks_meeting_kind():
    data = _fresh()
    anchors = se.phase_activity_anchors(_opp_phase_def(data, "商談準備"))
    kinds = {a["desc"]: a["kind"] for a in anchors}
    assert kinds["初回面談を実施"] == "meeting"
    assert kinds["初回面談を打診"] == ""


def test_phase_activity_anchors_tolerates_legacy_bare_strings():
    pdef = {"activity_template": ["A面談を実施", "B"]}
    assert se.phase_activity_anchors(pdef) == [
        {"desc": "A面談を実施", "kind": ""}, {"desc": "B", "kind": ""}]


def test_instantiate_seeds_unscheduled_meeting_for_meeting_anchor():
    data = _fresh()
    oid = se.opportunity_add(data, "Deal")  # starts in 商談準備
    se.instantiate_phase_activities(data, oid)
    opp = se.find_opportunity(data, oid)
    meet_act = next(a for a in opp["activities"] if a["description"] == "初回面談を実施")
    o, m = se.find_meeting_by_linked(data, meet_act["id"])
    assert m is not None
    assert m["status"] == se.MEETING_UNSCHEDULED
    assert m["scheduled_at"] == ""
    assert m["linked_id"] == meet_act["id"]
    # a non-meeting anchor gets no Meeting
    non = next(a for a in opp["activities"] if a["description"] == "初回面談を打診")
    assert se.find_meeting_by_linked(data, non["id"]) == (None, None)


def test_instantiate_idempotent_no_duplicate_meeting():
    data = _fresh()
    oid = se.opportunity_add(data, "Deal")
    se.instantiate_phase_activities(data, oid)
    se.instantiate_phase_activities(data, oid)  # re-enter phase
    opp = se.find_opportunity(data, oid)
    assert len(opp.get("meetings", [])) == 1  # not duplicated


def test_seeded_meeting_confirmed_by_reschedule_same_record():
    data = _fresh()
    oid = se.opportunity_add(data, "Deal")
    se.instantiate_phase_activities(data, oid)
    opp = se.find_opportunity(data, oid)
    mtg = opp["meetings"][0]
    assert mtg["status"] == se.MEETING_UNSCHEDULED
    se.meeting_reschedule(data, mtg["id"], "2026-08-01T10:00")
    assert mtg["status"] == se.MEETING_SCHEDULED
    assert mtg["scheduled_at"] == "2026-08-01T10:00"
    assert len(opp["meetings"]) == 1  # updated, not newly created


def test_scan_ended_meetings_ignores_unscheduled():
    data = _fresh()
    oid = se.opportunity_add(data, "Deal")
    se.instantiate_phase_activities(data, oid)  # seeds a 予定未定 meeting
    assert se.scan_ended_meetings(data, "2030-01-01T00:00") == []


def test_meeting_seed_unknown_opp_raises():
    data = _fresh()
    with pytest.raises(ValueError):
        se.meeting_seed(data, "opp-999")


# --- Advance gate (前進ゲート) — e-3579 entity + lifecycle ------------------

def test_opportunity_add_opens_initial_gate():
    # e-3580 fold: creating a deal opens the initial phase's gate (adv-1).
    data = _fresh()
    oid = se.opportunity_add(data, "Deal", phase="提案準備", created_at="T0")
    g = se.current_gate(data, oid)
    assert g["id"] == "adv-1" and g["status"] == se.GATE_OPEN
    assert g["phase"] == "提案準備"
    assert g["anchor"] == "" and g["outcome"] is None
    assert g["history"][0]["action"] == "opened"


def test_open_second_gate_while_one_open_raises():
    data = _fresh()
    oid = se.opportunity_add(data, "Deal")  # already has an open gate
    with pytest.raises(ValueError):
        se.open_advance_gate(data, oid, at="T1")  # ≤1 open — structural (AC1)


def test_settled_gate_frees_slot_for_next():
    data = _fresh()
    oid = se.opportunity_add(data, "Deal", phase="商談準備", created_at="T0")
    g1 = se.current_gate(data, oid)["id"]
    se.settle_gate(data, g1, outcome=se.GATE_ADVANCE, reason="初回面談OK",
                   actor="alice", at="T1")
    # a done gate no longer occupies the single open slot
    assert se.current_gate(data, oid) is None
    g2 = se.open_advance_gate(data, oid, phase="提案準備", at="T2")
    assert se.current_gate(data, oid)["id"] == g2


def test_gate_ids_global_across_opportunities():
    data = _fresh()
    o1 = se.opportunity_add(data, "D1")
    o2 = se.opportunity_add(data, "D2")
    # each deal's initial gate gets a globally-unique id
    assert se.current_gate(data, o1)["id"] == "adv-1"
    assert se.current_gate(data, o2)["id"] == "adv-2"


def test_settle_gate_stamps_outcome_and_evidence():
    data = _fresh()
    oid = se.opportunity_add(data, "Deal", phase="商談準備", created_at="T0")
    gid = se.current_gate(data, oid)["id"]
    se.settle_gate(data, gid, outcome=se.GATE_RETRY, reason="返事待ち",
                   actor="bob", at="T1")
    _, g = se.find_gate(data, gid)
    assert g["status"] == se.GATE_DONE and g["outcome"] == se.GATE_RETRY
    row = g["history"][-1]
    assert row["kind"] == "settled" and row["actor"] == "bob"
    assert row["at"] == "T1" and row["reason"] == "返事待ち" and row["outcome"] == "retry"


def test_settle_gate_rejects_bad_outcome_and_double_settle():
    data = _fresh()
    oid = se.opportunity_add(data, "Deal")
    gid = se.current_gate(data, oid)["id"]
    with pytest.raises(ValueError):
        se.settle_gate(data, gid, outcome="nope")
    se.settle_gate(data, gid, outcome=se.GATE_TERMINAL, at="T1")
    with pytest.raises(ValueError):
        se.settle_gate(data, gid, outcome=se.GATE_ADVANCE)  # already done


def test_ensure_open_gate_is_idempotent():
    data = _fresh()
    oid = se.opportunity_add(data, "Deal")   # opens adv-1
    g_a = se.ensure_open_gate(data, oid, at="T0")
    g_b = se.ensure_open_gate(data, oid, at="T1")   # no second gate created
    assert g_a["id"] == g_b["id"]
    opp = se.find_opportunity(data, oid)
    assert len(opp["gates"]) == 1


def test_anchor_gate_links_work_item_and_validates():
    data = _fresh()
    oid = se.opportunity_add(data, "Deal")
    act = se.activity_add(data, oid, "初回面談を打診")
    gid = se.current_gate(data, oid)["id"]
    se.anchor_gate(data, gid, act, at="T1")
    _, g = se.find_gate(data, gid)
    assert g["anchor"] == act
    assert g["history"][-1] == {"at": "T1", "action": "anchored", "anchor": act}
    with pytest.raises(ValueError):
        se.anchor_gate(data, gid, "opp-1")   # not a work item
    with pytest.raises(ValueError):
        se.anchor_gate(data, gid, "act-999")  # unknown work item


def test_anchor_requires_open_gate():
    data = _fresh()
    oid = se.opportunity_add(data, "Deal")
    act = se.activity_add(data, oid, "act")
    gid = se.current_gate(data, oid)["id"]
    se.settle_gate(data, gid, outcome=se.GATE_ADVANCE, at="T1")
    with pytest.raises(ValueError):
        se.anchor_gate(data, gid, act)  # done gate can't be anchored


def test_cancel_gate_frees_slot_and_is_audited():
    data = _fresh()
    oid = se.opportunity_add(data, "Deal")
    gid = se.current_gate(data, oid)["id"]
    se.cancel_gate(data, gid, reason="商談中止")
    _, g = se.find_gate(data, gid)
    assert g["status"] == se.GATE_CANCELLED
    assert g["meta"]["cancel_reason"] == "商談中止" and g["meta"]["cancelled_by"]
    # a cancelled gate is not open, so a new one can be opened
    assert se.current_gate(data, oid) is None
    assert se.open_advance_gate(data, oid, at="T1")  # succeeds (returns a new id)


def test_gate_history_returns_only_done_in_order():
    data = _fresh()
    oid = se.opportunity_add(data, "Deal", phase="商談準備", created_at="T0")
    g1 = se.current_gate(data, oid)["id"]
    se.settle_gate(data, g1, outcome=se.GATE_ADVANCE, at="T1")
    g2 = se.open_advance_gate(data, oid, phase="提案準備", at="T2")
    se.settle_gate(data, g2, outcome=se.GATE_ADVANCE, at="T3")
    se.open_advance_gate(data, oid, phase="先方検討中", at="T4")  # still open
    hist = se.gate_history(data, oid)
    assert [g["phase"] for g in hist] == ["商談準備", "提案準備"]


def test_current_gate_present_after_add_and_unknown_raises():
    data = _fresh()
    oid = se.opportunity_add(data, "Deal")
    assert se.current_gate(data, oid) is not None   # initial gate is open
    assert se.find_gate(data, "adv-9") == (None, None)
    with pytest.raises(ValueError):
        se.current_gate(data, "opp-999")


# --- e-3580 migration: legacy deal (opp-held fields) → gate列 (AC8) ----------

def _legacy_opp(data, *, phase, transition_date="", phase_seq=None):
    """Append a pre-fold Opportunity carrying transition_date / phase_history
    directly and no ``gates`` — the shape existing data has on disk."""
    seq = phase_seq or [phase]
    data.setdefault("opportunities", []).append({
        "id": se.next_opportunity_id(data), "title": "Legacy", "account_id": None,
        "phase": phase, "status": se._opportunity_status_for_phase(data, phase),
        "phase_history": [{"phase": p, "at": f"T{i}", "note": ""}
                          for i, p in enumerate(seq)],
        "transition_date": transition_date,
        "transition_date_history": ([{"transition_date": transition_date, "at": "T0"}]
                                    if transition_date else []),
        "activities": [],
    })
    return data["opportunities"][-1]["id"]


def test_migration_non_terminal_folds_date_onto_open_gate():
    data = _fresh()
    oid = _legacy_opp(data, phase="提案準備", transition_date="2026-09-01",
                      phase_seq=["商談準備", "提案準備"])
    # touching a gate accessor migrates on the fly (AC8)
    assert se.get_transition_date(data, oid) == "2026-09-01"
    o = se.find_opportunity(data, oid)
    assert "phase_history" not in o and "transition_date" not in o
    assert "transition_date_history" not in o
    # 商談準備 became a done(advance) gate; 提案準備 is the open gate w/ the date
    g = se.current_gate(data, oid)
    assert g["phase"] == "提案準備" and g["transition_date"] == "2026-09-01"
    assert [x["phase"] for x in se.phase_change_history(data, oid)] == ["商談準備"]


def test_migration_terminal_deal_has_no_open_gate():
    data = _fresh()
    oid = _legacy_opp(data, phase="失注", phase_seq=["商談準備", "提案準備", "失注"])
    assert se.current_gate(data, oid) is None          # 決着済み — nothing open
    hist = se.gate_history(data, oid)
    # 提案準備 was where the deal was 決着 from → outcome=terminal
    assert hist[-1]["phase"] == "提案準備" and hist[-1]["outcome"] == "terminal"
    assert se.transition_status(data, oid, "2027-01-01") == se.TRANSITION_SETTLED


def test_migration_is_idempotent_and_skips_new_deals():
    data = _fresh()
    new = se.opportunity_add(data, "New")   # already gated
    gates_before = se.find_opportunity(data, new)["gates"]
    se._migrate_opp_gates(data, se.find_opportunity(data, new))
    assert se.find_opportunity(data, new)["gates"] is gates_before  # untouched


# --- e-3581: judgement fires off the gate's anchored work-item (SPEC §2) -----

def test_transition_signal_vocabulary_removed():
    # e-3581: the per-phase judgement-method branch is gone.
    assert not hasattr(se, "SIGNAL_MANUAL")
    assert not hasattr(se, "phase_transition_signal")
    for p in se.DEFAULT_OPPORTUNITY_PHASES:
        assert "transition_signal" not in p


def test_work_item_completed_meeting_and_activity():
    data = _fresh()
    oid = se.opportunity_add(data, "Deal")
    mid = se.meeting_schedule(data, oid, "2026-08-01T14:00:00+09:00",
                              end_at="2026-08-01T15:00:00+09:00", at="T0")
    # scheduled but not yet past end
    assert se.work_item_completed(data, mid, "2026-08-01T14:30:00+09:00") is False
    # past its end → complete
    assert se.work_item_completed(data, mid, "2026-08-01T16:00:00+09:00") is True
    # explicitly ended → complete regardless of now
    se.meeting_mark_ended(data, mid, at="T1")
    assert se.work_item_completed(data, mid, "") is True
    # activity: complete only when done
    act = se.activity_add(data, oid, "提案書を準備")
    assert se.work_item_completed(data, act, "") is False
    se.activity_set_status(data, act, "done", at="T2")
    assert se.work_item_completed(data, act, "") is True


def test_gate_judgement_ready_only_fires_on_the_anchor():
    data = _fresh()
    oid = se.opportunity_add(data, "Deal")
    gid = se.current_gate(data, oid)["id"]
    anchored = se.meeting_schedule(data, oid, "2026-08-01T14:00:00+09:00",
                                   end_at="2026-08-01T15:00:00+09:00", at="T0")
    stray = se.meeting_schedule(data, oid, "2026-08-02T14:00:00+09:00",
                                end_at="2026-08-02T15:00:00+09:00", at="T0")
    se.anchor_gate(data, gid, anchored, at="T0")
    # a stray (unanchored) meeting ending does NOT make the gate ready (AC2).
    # now is before the anchored meeting's own end, so only the stray is "over".
    se.meeting_mark_ended(data, stray, at="T1")
    assert se.gate_judgement_ready(data, oid, "2026-08-01T14:30:00+09:00") is False
    # the anchored meeting ending does (AC2) — ended status fires regardless of now.
    se.meeting_mark_ended(data, anchored, at="T2")
    assert se.gate_judgement_ready(data, oid, "2026-08-01T14:30:00+09:00") is True


def test_gate_judgement_ready_activity_anchor_and_date_fallback():
    data = _fresh()
    # dated activity anchor: its done fires judgement, same as a meeting (AC3)
    oid = se.opportunity_add(data, "Deal")
    gid = se.current_gate(data, oid)["id"]
    act = se.activity_add(data, oid, "合意の確認を取る", deadline="2026-08-10")
    se.anchor_gate(data, gid, act, at="T0")
    assert se.get_transition_date(data, oid) == "2026-08-10"   # AC4 date sync
    assert se.gate_judgement_ready(data, oid, "2026-08-11T00:00:00Z") is False
    se.activity_set_status(data, act, "done", at="T1")
    assert se.gate_judgement_ready(data, oid, "2026-08-11T00:00:00Z") is True
    # unanchored gate falls back to 遷移日 due/overdue (縮退 manual)
    oid2 = se.opportunity_add(data, "Deal2", transition_date="2026-08-01")
    assert se.gate_judgement_ready(data, oid2, "2026-07-01T00:00:00Z") is False
    assert se.gate_judgement_ready(data, oid2, "2026-08-01T00:00:00Z") is True


def test_awaiting_gate_judgement_lists_ready_deals_oldest_first():
    data = _fresh()
    a = se.opportunity_add(data, "A", transition_date="2026-08-05")
    b = se.opportunity_add(data, "B", transition_date="2026-08-01")
    se.opportunity_add(data, "C", transition_date="2026-12-31")  # not due yet
    rows = se.opportunities_awaiting_gate_judgement(data, "2026-08-10T00:00:00Z")
    assert [r["id"] for r in rows] == [b, a]   # oldest 遷移日 first, C excluded
