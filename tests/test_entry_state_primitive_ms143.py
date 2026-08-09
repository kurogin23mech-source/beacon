"""Unit tests for the ms-143 profession-generic done/find primitives
(``occupation.find_target_entry`` / ``set_entry_state``, 設計判断 b 系統3).

これが『1 本の抽象 find+set が両職種の work-item を服す』の証明: dev の task
(milestone の entries、ネストした subtask 含む) と sales の activity (opportunity の
activities) が、profession を分岐せず同じ ``find_target_entry`` で見つかり、同じ
``set_entry_state`` で done/todo 遷移する。done の完了 attribution (done_by/
done_reason) が刻まれること、cancelled は audit-stamp 経路に回すため拒否されること
も pin する。

parity: find は core.find_entry の nested 再帰と set は work_model.mark_done の
done スタンプ契約に一致する (wiring 段で core.task_done / activity_set_status を
これらに寄せても挙動不変であることの土台)。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

LIB = Path(__file__).parent.parent / "lib"
sys.path.insert(0, str(LIB))

import occupation  # noqa: E402
import work_base   # noqa: E402
import work_model  # noqa: E402

FIXED_TS = "2026-08-09T00:00:00Z"
ACTOR = "test-actor"


@pytest.fixture(autouse=True)
def _freeze(monkeypatch):
    monkeypatch.setattr(work_base, "now_iso", lambda: FIXED_TS)
    monkeypatch.setattr(work_base, "current_actor", lambda: ACTOR)


def _dev():
    return {"profession": "dev", "milestones": [
        {"id": "ms-1", "label": "M", "status": "in_progress", "entries": [
            {"id": "e-1", "type": "task", "description": "top", "status": "todo"},
            {"id": "e-2", "type": "task", "description": "parent", "status": "todo",
             "entries": [
                 {"id": "e-3", "type": "task", "description": "child", "status": "todo"},
             ]},
        ]},
    ]}


def _sales():
    return {"profession": "sales", "opportunities": [
        {"id": "opp-1", "label": "O", "phase": "lead", "activities": [
            {"id": "act-1", "description": "訪問", "status": "todo"},
        ]},
    ]}


# --- find_target_entry parity ---------------------------------------------------

def test_find_dev_top_level_task():
    data = _dev()
    hit = occupation.find_target_entry(data, "e-1")
    assert hit is not None
    target, arm_list, entry, idx = hit
    assert target["id"] == "ms-1"
    assert entry["id"] == "e-1"
    assert arm_list is data["milestones"][0]["entries"]
    assert idx == 0


def test_find_dev_nested_subtask():
    """core.find_entry と同じく nested entries に再帰到達する。"""
    data = _dev()
    hit = occupation.find_target_entry(data, "e-3")
    assert hit is not None
    target, arm_list, entry, idx = hit
    assert target["id"] == "ms-1"
    assert entry["description"] == "child"
    # arm_list は直近の親リスト (= e-2 の nested entries)、idx はその中の位置
    assert arm_list is data["milestones"][0]["entries"][1]["entries"]
    assert idx == 0


def test_find_sales_activity():
    data = _sales()
    hit = occupation.find_target_entry(data, "act-1")
    assert hit is not None
    target, arm_list, entry, idx = hit
    assert target["id"] == "opp-1"
    assert entry["id"] == "act-1"
    assert arm_list is data["opportunities"][0]["activities"]


def test_find_missing_returns_none():
    assert occupation.find_target_entry(_dev(), "e-999") is None


# --- set_entry_state parity -----------------------------------------------------

def test_set_done_stamps_completion_attribution_dev():
    data = _dev()
    target, entry = occupation.set_entry_state(
        data, "e-1", "done", at=FIXED_TS, actor=ACTOR, reason="finished")
    assert target["id"] == "ms-1"
    assert entry["status"] == work_model.DONE_STATUS
    assert entry["done_at"] == FIXED_TS
    assert entry["meta"]["done_by"] == ACTOR
    assert entry["meta"]["done_reason"] == "finished"


def test_set_done_sales_activity_same_stamp():
    data = _sales()
    _target, act = occupation.set_entry_state(
        data, "act-1", "done", at=FIXED_TS, actor=ACTOR)
    assert act["status"] == work_model.DONE_STATUS
    assert act["done_at"] == FIXED_TS
    assert act["meta"]["done_by"] == ACTOR


def test_set_todo_is_plain_status_set():
    data = _dev()
    # first complete, then reopen to todo — todo must NOT carry a done stamp
    occupation.set_entry_state(data, "e-1", "done", actor=ACTOR)
    _t, entry = occupation.set_entry_state(data, "e-1", "todo")
    assert entry["status"] == work_model.TODO_STATUS


def test_set_nested_subtask_done():
    data = _dev()
    _t, entry = occupation.set_entry_state(data, "e-3", "done", actor=ACTOR)
    assert entry["status"] == work_model.DONE_STATUS
    assert data["milestones"][0]["entries"][1]["entries"][0]["status"] == "done"


def test_set_cancelled_rejected():
    """cancelled は audit-stamp 経路 (work_base.stamp_cancel) 専用なので bare set
    を拒否 (activity_set_status の _SETTABLE ガードと同じ)。"""
    with pytest.raises(ValueError):
        occupation.set_entry_state(_dev(), "e-1", "cancelled", actor=ACTOR)


def test_set_missing_raises():
    with pytest.raises(ValueError):
        occupation.set_entry_state(_dev(), "e-999", "done")
