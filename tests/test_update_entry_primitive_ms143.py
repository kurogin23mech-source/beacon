"""Unit + parity tests for the ms-143 profession-generic attribute-patch
primitive (``occupation.update_entry``, 設計判断 i = 更新).

``update_entry`` is the attribute-patch sibling of ``set_entry_state`` (lifecycle
todo/done): it locates a Target OR a work item by id — generically, never naming
``data['milestones']`` / ``data['opportunities']`` — and applies a plain field
patch. Per-field validation stays in the frontend (mirrors ``add_work_item``).

The sales attribute-patch setters (amount / rename / describe) now route their
locate + patch through this primitive, so the 更新 path stops naming
``find_opportunity`` / ``data['opportunities']``. These harnesses PIN that the
rerouted setters keep byte-identical behaviour (incl. clear-by-None, empty-title
rejection, unknown-id ValueError). The rich dev ``milestone update`` and the sales
``opportunity phase`` transition are DEFERRED (they don't fit a plain patch) and
keep their own frontend path — not exercised here.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

LIB = Path(__file__).parent.parent / "lib"
sys.path.insert(0, str(LIB))

import occupation      # noqa: E402
import sales_entities  # noqa: E402


def _sales():
    return {
        "id": "p", "profession": "sales",
        "opportunities": [
            {"id": "opp-1", "title": "Deal", "phase": "商談中", "goal_amount": 100,
             "activities": [{"id": "act-1", "description": "call", "status": "todo"}]},
        ],
    }


def _dev():
    return {
        "id": "p", "profession": "dev",
        "milestones": [
            {"id": "ms-1", "label": "M", "status": "in_progress", "entries": [
                {"id": "e-1", "type": "task", "description": "a", "status": "todo"},
            ]},
        ],
    }


# --- generic primitive: target + work-item, plain patch, None clears -------

def test_patches_a_target_by_id():
    data = _sales()
    rec = occupation.update_entry(data, "opp-1", goal_amount=500, title="Renamed")
    assert rec is data["opportunities"][0]
    assert rec["goal_amount"] == 500 and rec["title"] == "Renamed"


def test_patches_a_work_item_by_id():
    data = _sales()
    rec = occupation.update_entry(data, "act-1", deadline="2026-09-01")
    assert rec is data["opportunities"][0]["activities"][0]
    assert rec["deadline"] == "2026-09-01"


def test_none_value_clears_a_field():
    data = _sales()
    occupation.update_entry(data, "opp-1", goal_amount=None)
    assert data["opportunities"][0]["goal_amount"] is None  # written, not skipped


def test_only_passed_keys_are_touched():
    data = _sales()
    occupation.update_entry(data, "opp-1", goal_amount=999)
    assert data["opportunities"][0]["title"] == "Deal"  # untouched


def test_locates_a_dev_milestone_and_task_too():
    data = _dev()
    occupation.update_entry(data, "ms-1", target_date="2026-12-31")
    assert data["milestones"][0]["target_date"] == "2026-12-31"
    occupation.update_entry(data, "e-1", priority="high")
    assert data["milestones"][0]["entries"][0]["priority"] == "high"


def test_unknown_id_raises():
    with pytest.raises(ValueError, match="Entry not found"):
        occupation.update_entry(_sales(), "opp-404", title="x")


# --- rerouted sales setters keep byte-identical behaviour ------------------

def test_set_amount_sets_and_clears_via_primitive():
    data = _sales()
    sales_entities.set_opportunity_amount(data, "opp-1", 1500000)
    assert data["opportunities"][0]["goal_amount"] == 1500000
    sales_entities.set_opportunity_amount(data, "opp-1", None)
    assert data["opportunities"][0]["goal_amount"] is None
    with pytest.raises(ValueError):
        sales_entities.set_opportunity_amount(data, "opp-99", 1)


def test_set_title_renames_rejects_empty_and_unknown():
    data = _sales()
    opp = sales_entities.opportunity_set_title(data, "opp-1", "  New Name  ")
    assert opp["title"] == "New Name"
    with pytest.raises(ValueError):
        sales_entities.opportunity_set_title(data, "opp-1", "   ")
    with pytest.raises(ValueError):
        sales_entities.opportunity_set_title(data, "opp-nope", "x")


def test_set_description_sets_clears_and_unknown():
    data = _sales()
    sales_entities.opportunity_set_description(data, "opp-1", "背景メモ")
    assert data["opportunities"][0]["description"] == "背景メモ"
    sales_entities.opportunity_set_description(data, "opp-1", "")
    assert data["opportunities"][0]["description"] == ""
    with pytest.raises(ValueError):
        sales_entities.opportunity_set_description(data, "opp-999", "x")
