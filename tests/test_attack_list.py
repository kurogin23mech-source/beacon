"""Unit tests for the attack-list schema (ms-132 e-4501).

Two layers:
1. The canonical column schema shape (keys / types / configurable phase enum) and
   ``is_attack_list`` recognition.
2. That the schema, run through the *real* ms-131 table-doc + type-checking layer,
   actually enforces the intended constraints — an ``acc-`` Account ref, a phase
   inside the funnel, an ISO date — so downstream tasks (e-4503..e-4506) can trust
   a row's shape rather than re-validating.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import pytest  # noqa: E402

import attack_list  # noqa: E402
import table_doc  # noqa: E402
import table_type  # noqa: E402


# --- schema shape ----------------------------------------------------------

def test_default_columns_shape():
    cols = attack_list.attack_list_columns()
    by_key = {c["key"]: c for c in cols}
    assert set(by_key) == {"account", "phase", "last_contact", "memo"}
    assert by_key["account"]["type"] == "ref"
    assert by_key["account"]["ref"] == "acc"  # pinned to the Account id-space
    assert by_key["phase"]["type"] == "enum"
    assert by_key["phase"]["values"] == ["未接触", "連絡済", "返信あり", "アポ"]
    assert by_key["last_contact"]["type"] == "date"
    assert by_key["memo"]["type"] == "text"


def test_phase_override_is_applied():
    cols = attack_list.attack_list_columns(["A", "B", "C"])
    phase = {c["key"]: c for c in cols}["phase"]
    assert phase["values"] == ["A", "B", "C"]


def test_none_phases_falls_back_to_default():
    cols = attack_list.attack_list_columns(None)
    phase = {c["key"]: c for c in cols}["phase"]
    assert phase["values"] == attack_list.DEFAULT_PROSPECT_PHASES


def test_empty_phases_is_rejected():
    with pytest.raises(ValueError):
        attack_list.attack_list_columns([])


def test_initial_phase_is_first_default():
    assert attack_list.INITIAL_PROSPECT_PHASE == "未接触"
    assert attack_list.INITIAL_PROSPECT_PHASE == attack_list.DEFAULT_PROSPECT_PHASES[0]


# --- is_attack_list recognition -------------------------------------------

def test_is_attack_list_true_for_canonical():
    assert attack_list.is_attack_list(attack_list.attack_list_columns())


def test_is_attack_list_true_with_extra_column():
    cols = attack_list.attack_list_columns()
    cols.append({"key": "owner", "type": "text"})  # a project added a column
    assert attack_list.is_attack_list(cols)


def test_is_attack_list_false_without_ref_account():
    cols = attack_list.attack_list_columns()
    # account present but not a ref → not an outreach list
    for c in cols:
        if c["key"] == "account":
            c["type"] = "text"
    assert not attack_list.is_attack_list(cols)


def test_is_attack_list_false_for_unrelated_table():
    assert not attack_list.is_attack_list(
        [{"key": "name", "type": "text"}, {"key": "amount", "type": "number"}])


def test_is_attack_list_empty_or_none():
    assert not attack_list.is_attack_list([])
    assert not attack_list.is_attack_list(None)


# --- is_reacted / phase_values / find_row_by_account helpers ---------------

def test_is_reacted():
    vals = ["未接触", "連絡済", "返信あり", "アポ"]
    assert not attack_list.is_reacted("未接触", vals)
    assert not attack_list.is_reacted("連絡済", vals)
    assert attack_list.is_reacted("返信あり", vals)
    assert attack_list.is_reacted("アポ", vals)
    assert not attack_list.is_reacted("連絡済", ["未接触", "連絡済"])  # no REPLIED position


def test_phase_values_from_model_and_columns():
    cols = attack_list.attack_list_columns()
    assert attack_list.phase_values(cols) == attack_list.DEFAULT_PROSPECT_PHASES
    assert attack_list.phase_values({"columns": cols}) == attack_list.DEFAULT_PROSPECT_PHASES
    assert attack_list.phase_values({"columns": []}) == []


def test_find_row_by_account():
    rows = [{"id": "r1", "cells": {"account": "acc-1"}},
            {"id": "r2", "cells": {"account": "acc-2"}}]
    assert attack_list.find_row_by_account(rows, "acc-2")["id"] == "r2"
    assert attack_list.find_row_by_account(rows, "acc-9") is None


# --- enforcement through the real table-doc + type layer -------------------

@pytest.fixture
def typed_table():
    """A fresh attack-list table with the ms-131 type checker installed."""
    table_type.install()
    try:
        yield table_doc.new_table(attack_list.attack_list_columns())
    finally:
        table_type.uninstall()


def test_valid_prospect_row_is_accepted(typed_table):
    rid = table_doc.add_row(
        typed_table,
        {"account": "acc-7", "phase": "未接触",
         "last_contact": "2026-07-29", "memo": "初回打診予定"},
        actor="claude", at="T0")
    assert table_doc.get_row(typed_table, rid)["cells"]["account"] == "acc-7"


def test_non_account_ref_is_rejected(typed_table):
    # 方針6: an attack-list row must point at an Account (acc-), not e.g. an opp-.
    with pytest.raises(table_doc.TableDocError):
        table_doc.add_row(typed_table, {"account": "opp-3", "phase": "未接触"},
                          actor="claude", at="T0")


def test_phase_outside_funnel_is_rejected(typed_table):
    with pytest.raises(table_doc.TableDocError):
        table_doc.add_row(typed_table, {"account": "acc-1", "phase": "成約"},
                          actor="claude", at="T0")


def test_bad_date_is_rejected(typed_table):
    with pytest.raises(table_doc.TableDocError):
        table_doc.add_row(
            typed_table,
            {"account": "acc-1", "phase": "未接触", "last_contact": "2026/07/29"},
            actor="claude", at="T0")


def test_phase_transition_records_history(typed_table):
    rid = table_doc.add_row(typed_table, {"account": "acc-1", "phase": "未接触"},
                            actor="claude", at="T0")
    table_doc.set_cell(typed_table, rid, "phase", "連絡済",
                       actor="claude", at="T1")
    hist = table_doc.get_row(typed_table, rid)["history"][-1]
    assert (hist["old"], hist["new"]) == ("未接触", "連絡済")
