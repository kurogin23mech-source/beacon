"""Unit tests for the sales job-template entity model (ms-106 ①).

Covers the minimum data layer: Account (+nested Contact), Opportunity with a
参照 association to Account, append-only phase_history, and Activity as a
従属 composition child. Also pins that a sales-template project passes the
shared ``core.validate_project`` unchanged (= no dev-path regression).
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


def test_sales_template_passes_shared_validator():
    # The empty milestones[] is what lets the sales project through the dev
    # validator without touching it. Must not raise.
    core.validate_project(se.build_sales_project("S", "obj"))


# --- Account + Contact -----------------------------------------------------

def test_account_add_and_id_sequence():
    data = _fresh()
    a1 = se.account_add(data, "Globex")
    a2 = se.account_add(data, "Initech")
    assert a1 == "acc-1"
    assert a2 == "acc-2"
    assert se.find_account(data, "acc-1")["name"] == "Globex"


def test_account_name_required():
    data = _fresh()
    with pytest.raises(ValueError):
        se.account_add(data, "   ")


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


# --- Opportunity + 参照 association ----------------------------------------

def test_opportunity_add_seeds_phase_history():
    data = _fresh()
    opp = se.opportunity_add(data, "Big deal", phase="lead", created_at="T0")
    assert opp == "opp-1"
    o = se.find_opportunity(data, opp)
    assert o["phase"] == "lead"
    assert o["status"] == "open"
    # phase_history starts non-empty (evidence series never empty).
    assert o["phase_history"] == [{"phase": "lead", "at": "T0", "note": "initial"}]


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


# --- append-only phase transitions -----------------------------------------

def test_phase_set_is_append_only():
    data = _fresh()
    opp = se.opportunity_add(data, "Deal", phase="lead", created_at="T0")
    se.phase_set(data, opp, "qualified", note="fit confirmed", at="T1")
    se.phase_set(data, opp, "proposal", at="T2")
    o = se.find_opportunity(data, opp)
    # current (exclusive) phase reflects the latest declaration...
    assert o["phase"] == "proposal"
    # ...and the full traversal is retained (AC12: both are queryable).
    assert [h["phase"] for h in o["phase_history"]] == ["lead", "qualified", "proposal"]


def test_phase_set_terminal_mirrors_status():
    data = _fresh()
    opp = se.opportunity_add(data, "Deal")
    se.phase_set(data, opp, "won", at="T9")
    assert se.find_opportunity(data, opp)["status"] == "won"


def test_phase_set_unknown_opportunity():
    data = _fresh()
    with pytest.raises(ValueError):
        se.phase_set(data, "opp-9", "won")


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
    a1 = se.activity_add(data, o1, "call")
    a2 = se.activity_add(data, o2, "email")
    assert a1 == "act-1"
    assert a2 == "act-2"  # global counter, not per-opportunity


def test_activity_unknown_opportunity():
    data = _fresh()
    with pytest.raises(ValueError):
        se.activity_add(data, "opp-9", "call")
