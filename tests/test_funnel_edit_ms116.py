"""Unit tests for ms-116 phase-funnel editing primitives.

ms-116 lets a *running* sales project edit its saved phase funnel
(``account_phases`` / ``opportunity_phases``) — add / insert / rename /
reorder / delete stages — without rebuilding the project. These pin the lib
primitives in ``sales_entities`` (task e-3820), covering:

  - the funnel entry (既定フェーズ) re-derives from the reordered list (AC4),
  - rename follows every reference so no target keeps a dead pointer (AC5),
  - delete is blocked while live targets still sit in the stage (AC3),
  - the same primitives edit both the account and opportunity funnels (AC2).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

import sales_entities as se  # noqa: E402


def _fresh():
    return se.build_sales_project("Acme Sales", "close deals")


def _names(data, kind):
    key = "account_phases" if kind == "account" else "opportunity_phases"
    return [p["name"] for p in data[key]]


# --- add / insert ----------------------------------------------------------

def test_add_phase_appends_to_end():
    data = _fresh()
    se.add_phase(data, "account", "休眠")
    assert _names(data, "account") == ["未接触", "リード", "未成約顧客", "成約顧客", "休眠"]


def test_insert_phase_at_head_becomes_funnel_entry():
    # AC4: inserting at index 0 makes the new stage the funnel entry, and the
    # order-dependent derived getter re-derives it with no extra step.
    data = _fresh()
    # start from a project that lacks 未接触 (the ms-115 pre-existing shape)
    data["account_phases"] = [{"name": "リード"}, {"name": "未成約顧客"}, {"name": "成約顧客"}]
    assert se.default_account_phase(data) == "リード"
    se.insert_phase(data, "account", "未接触", index=0)
    assert _names(data, "account") == ["未接触", "リード", "未成約顧客", "成約顧客"]
    assert se.default_account_phase(data) == "未接触"


def test_insert_phase_middle_and_past_end():
    data = _fresh()
    se.insert_phase(data, "account", "商談中", index=2)
    assert _names(data, "account")[2] == "商談中"
    se.insert_phase(data, "account", "末尾", index=999)  # past end appends
    assert _names(data, "account")[-1] == "末尾"


def test_insert_rejects_duplicate_empty_and_bad_index():
    data = _fresh()
    with pytest.raises(ValueError, match="already exists"):
        se.insert_phase(data, "account", "リード")
    with pytest.raises(ValueError, match="required"):
        se.insert_phase(data, "account", "   ")
    with pytest.raises(ValueError, match="out of range"):
        se.insert_phase(data, "account", "X", index=-1)


# --- rename (reference following, AC5) -------------------------------------

def test_rename_follows_account_references():
    data = _fresh()
    acc = se.account_add(data, "顧客A", phase="リード")
    se.rename_phase(data, "account", "リード", "見込み")
    assert "見込み" in _names(data, "account") and "リード" not in _names(data, "account")
    assert se.find_account(data, acc)["phase"] == "見込み"


def test_rename_follows_opportunity_references_including_cancelled():
    data = _fresh()
    opp = se.opportunity_add(data, "商談X", phase="提案準備")
    # a tombstoned (cancelled) row must stay internally consistent too
    dead = se.opportunity_add(data, "商談Y", phase="提案準備")
    se.find_opportunity(data, dead)["status"] = se.CANCELLED_STATUS
    se.rename_phase(data, "opportunity", "提案準備", "提案")
    assert se.find_opportunity(data, opp)["phase"] == "提案"
    assert se.find_opportunity(data, dead)["phase"] == "提案"


def test_rename_rejects_missing_and_collision():
    data = _fresh()
    with pytest.raises(ValueError, match="not found"):
        se.rename_phase(data, "account", "無い段", "X")
    with pytest.raises(ValueError, match="already exists"):
        se.rename_phase(data, "account", "リード", "成約顧客")
    # renaming to the same name is a no-op, not an error
    assert se.rename_phase(data, "account", "リード", "リード")["name"] == "リード"


# --- move / reorder --------------------------------------------------------

def test_move_phase_reorders_and_changes_entry():
    data = _fresh()
    se.move_phase(data, "account", "成約顧客", 0)
    assert _names(data, "account")[0] == "成約顧客"
    assert se.default_account_phase(data) == "成約顧客"


def test_move_phase_rejects_missing_and_bad_index():
    data = _fresh()
    with pytest.raises(ValueError, match="not found"):
        se.move_phase(data, "account", "無い段", 0)
    with pytest.raises(ValueError, match="out of range"):
        se.move_phase(data, "account", "リード", 99)


def test_move_opportunity_reorders_first_non_terminal_entry():
    data = _fresh()
    # default_opportunity_phase = first non-terminal; reordering stages moves it
    assert se.default_opportunity_phase(data) == "商談準備"
    se.move_phase(data, "opportunity", "提案準備", 0)
    assert se.default_opportunity_phase(data) == "提案準備"


# --- remove (delete-block, AC3) --------------------------------------------

def test_remove_empty_phase_succeeds():
    data = _fresh()
    se.remove_phase(data, "account", "成約顧客")
    assert "成約顧客" not in _names(data, "account")


def test_remove_blocks_non_empty_phase_with_informative_error():
    data = _fresh()
    se.account_add(data, "顧客A", phase="リード")
    se.account_add(data, "顧客B", phase="リード")
    with pytest.raises(ValueError, match="cannot delete non-empty") as ei:
        se.remove_phase(data, "account", "リード")
    msg = str(ei.value)
    assert "2 target" in msg and "acc-" in msg  # names count + ids
    assert "リード" in _names(data, "account")  # untouched


def test_remove_ignores_cancelled_occupants():
    # a cancelled row is a tombstone, not data you'd lose — it must not block.
    data = _fresh()
    opp = se.opportunity_add(data, "商談Z", phase="提案準備")
    se.find_opportunity(data, opp)["status"] = se.CANCELLED_STATUS
    se.remove_phase(data, "opportunity", "提案準備")
    assert "提案準備" not in _names(data, "opportunity")


def test_remove_rejects_missing():
    data = _fresh()
    with pytest.raises(ValueError, match="not found"):
        se.remove_phase(data, "account", "無い段")


# --- opportunity funnel with stage attrs (AC2) + unknown kind --------------

def test_opportunity_stage_carries_attrs():
    data = _fresh()
    se.insert_phase(data, "opportunity", "内諾", index=3,
                    probability=60, terminal=False, allowed_terminals=["成約", "失注"])
    pdef = se._find_phase_def(data["opportunity_phases"], "内諾")
    assert pdef["probability"] == 60 and pdef["terminal"] is False
    assert pdef["allowed_terminals"] == ["成約", "失注"]


def test_unknown_funnel_kind_raises():
    data = _fresh()
    with pytest.raises(ValueError, match="unknown funnel kind"):
        se.insert_phase(data, "milestone", "X")
