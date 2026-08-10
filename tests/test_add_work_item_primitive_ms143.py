"""Unit tests for the ms-143 profession-generic work-item primitive
(``occupation.add_work_item``, 設計判断 b 系統1 + 設計判断 a global-by-prefix 採番).

dev の task (milestone の entries、type="task") と sales の activity (opportunity の
activities、type なし) が、profession 分岐なしに同じ ``add_work_item`` で各自の arm に
生まれる。id は prefix ごとの GLOBAL 空間で採番され、既存の hand-rolled allocator
(core.next_entry_id が milestones+operations を走査、sales_entities.next_activity_id が
opportunities を走査) と【同じ結果】を出すことを parity で pin する (leader 握り)。

乖離が出たら silent に変えず surface する規律 (ms-142 terminal 差と同じ) — この harness が
その乖離検知器。乖離無し = 旧も (a) と同じ範囲を走査済、の証拠。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

LIB = Path(__file__).parent.parent / "lib"
sys.path.insert(0, str(LIB))

import occupation      # noqa: E402
import core            # noqa: E402
import sales_entities  # noqa: E402
import work_base       # noqa: E402
import work_model      # noqa: E402

FIXED_TS = "2026-08-09T00:00:00Z"


@pytest.fixture(autouse=True)
def _freeze(monkeypatch):
    monkeypatch.setattr(work_base, "now_iso", lambda: FIXED_TS)
    monkeypatch.setattr(work_base, "current_actor", lambda: "test-actor")


def _dev_rich():
    """milestones with nested subtasks + an operation carrying e- entries — the
    cross-collection e- space that a naive milestone-only scan would miss."""
    return {"id": "p", "profession": "dev",
            "milestones": [
                {"id": "ms-1", "label": "M", "status": "in_progress", "entries": [
                    {"id": "e-1", "type": "task", "description": "a", "status": "todo"},
                    {"id": "e-2", "type": "task", "description": "b", "status": "todo",
                     "entries": [
                         {"id": "e-3", "type": "task", "description": "c", "status": "todo"},
                     ]},
                ]},
            ],
            "operations": [
                {"id": "op-1", "entries": [
                    {"id": "e-4", "type": "save", "description": "run"},
                    {"id": "e-5", "type": "save", "description": "run2"},
                ]},
            ]}


def _sales_rich():
    return {"id": "p", "profession": "sales",
            "opportunities": [
                {"id": "opp-1", "label": "O", "phase": "lead", "activities": [
                    {"id": "act-1", "description": "x", "status": "todo"},
                    {"id": "act-2", "description": "y", "status": "done"},
                ]},
            ]}


def test_add_task_lands_under_milestone_with_type():
    data = _dev_rich()
    item = occupation.add_work_item(data, "ms-1", description="new task")
    assert item["type"] == "task"
    assert item["status"] == work_model.TODO_STATUS
    assert item["description"] == "new task"
    assert data["milestones"][0]["entries"][-1] is item


def test_add_activity_lands_under_opportunity_no_type():
    data = _sales_rich()
    item = occupation.add_work_item(data, "opp-1", description="visit")
    assert "type" not in item  # opportunity work_item_arm item_type is None
    assert item["status"] == work_model.TODO_STATUS
    assert data["opportunities"][0]["activities"][-1] is item


def test_extra_fields_ride_through():
    data = _sales_rich()
    item = occupation.add_work_item(
        data, "opp-1", description="call", deadline="2026-09-01",
        who_has_the_ball="them")
    assert item["deadline"] == "2026-09-01"
    assert item["who_has_the_ball"] == "them"


def test_missing_target_raises():
    with pytest.raises(ValueError, match="not found"):
        occupation.add_work_item(_dev_rich(), "ms-99", description="x")


# --- 採番 parity: (a) global-by-prefix == 旧 hand-rolled allocator -------------

def test_task_id_matches_next_entry_id_across_operations():
    """dev の e- 採番が core.next_entry_id (milestones+operations 走査) と一致。
    max は operation の e-5 なので naive な milestone-only 走査だと e-4 を返し
    衝突するが、global-by-prefix は e-6 を返す = next_entry_id と同結果。"""
    data = _dev_rich()
    expected = core.next_entry_id(data)          # 旧経路
    item = occupation.add_work_item(data, "ms-1", description="z")
    assert item["id"] == expected == "e-6"


def test_activity_id_matches_next_activity_id():
    data = _sales_rich()
    expected = sales_entities.next_activity_id(data)   # 旧経路
    item = occupation.add_work_item(data, "opp-1", description="z")
    assert item["id"] == expected == "act-3"


def test_no_prefix_crosstalk():
    """act- 採番は e- id を拾わない (prefix グローバル一意の前提)。"""
    data = {"id": "p", "profession": "sales",
            "opportunities": [{"id": "opp-1", "label": "O", "phase": "lead",
                               "activities": [{"id": "act-1", "description": "x"}]}],
            # a stray milestones collection with e- ids must NOT affect act- alloc
            "milestones": [{"id": "ms-1", "entries": [{"id": "e-9", "type": "task"}]}]}
    item = occupation.add_work_item(data, "opp-1", description="z")
    assert item["id"] == "act-2"
