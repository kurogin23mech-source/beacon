"""L2 締切エンジン (lib/deadline.py) の単体テスト — ms-139 e-4948.

締切 (deadline) を職種横断の L2 プリミティブにする第一歩。pure な temporal コアと、
work item / target を同一規則で overdue 判定する L2 アクセサ (deadline_of /
is_settled / work_item_temporal_status / overdue_work_items) を検証する。

規則の核 (SPEC 方針1・2): overdue = 「今日 > 締切 かつ status が terminal
(done/cancelled) でない」。フィールド名は職種ごと (activity/task = ``deadline``、
milestone = ``target_date``) に残し、規則だけ L2 で共通化する。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

import deadline  # noqa: E402
import sales_entities as se  # noqa: E402
import work_model  # noqa: E402

TODAY = "2026-08-09"


# --- pure temporal core -----------------------------------------------------

def test_temporal_status_classifies_by_date():
    assert deadline.temporal_status("", TODAY) == deadline.TRANSITION_UNSET
    assert deadline.temporal_status("2026-08-07", TODAY) == deadline.TRANSITION_OVERDUE
    assert deadline.temporal_status("2026-08-09", TODAY) == deadline.TRANSITION_DUE
    assert deadline.temporal_status("2026-08-20", TODAY) == deadline.TRANSITION_SCHEDULED


def test_temporal_status_settled_wins_over_date():
    # 決着済みは期日が過去でも SETTLED (= もう催促しない)。
    assert deadline.temporal_status(
        "2020-01-01", TODAY, settled=True) == deadline.TRANSITION_SETTLED


# --- L2 tolerant accessor: フィールド名は職種ごとに残す ---------------------

def test_deadline_of_reads_canonical_then_legacy():
    assert deadline.deadline_of({"deadline": "2026-08-07"}) == "2026-08-07"      # activity/task
    assert deadline.deadline_of({"target_date": "2026-08-07"}) == "2026-08-07"   # milestone(legacy)
    # canonical 優先: 両方あれば deadline を採る。
    assert deadline.deadline_of(
        {"deadline": "2026-08-07", "target_date": "2026-09-01"}) == "2026-08-07"
    assert deadline.deadline_of({}) == ""
    assert deadline.deadline_of({"deadline": "  2026-08-07  "}) == "2026-08-07"


def test_is_settled_matches_terminal_statuses():
    assert deadline.is_settled({"status": work_model.DONE_STATUS})
    assert deadline.is_settled({"status": work_model.CANCELLED_STATUS})
    assert not deadline.is_settled({"status": work_model.TODO_STATUS})
    assert not deadline.is_settled({})


# --- L2 規則が task / milestone / activity を同一規則で評価する --------------

def test_work_item_temporal_status_same_rule_across_classes():
    activity = {"deadline": "2026-08-07", "status": "todo"}
    milestone = {"target_date": "2026-08-07", "status": "in_progress"}   # legacy field
    task = {"deadline": "2026-08-09", "status": "todo"}                  # 新設 deadline
    assert deadline.work_item_temporal_status(activity, TODAY) == deadline.TRANSITION_OVERDUE
    assert deadline.work_item_temporal_status(milestone, TODAY) == deadline.TRANSITION_OVERDUE
    assert deadline.work_item_temporal_status(task, TODAY) == deadline.TRANSITION_DUE


def test_terminal_work_item_is_excluded_from_overdue():
    # done / cancelled は期日超過でも overdue にならない (= 完了で催促が止まる)。
    done = {"deadline": "2026-08-01", "status": work_model.DONE_STATUS}
    cancelled = {"deadline": "2026-08-01", "status": work_model.CANCELLED_STATUS}
    assert deadline.work_item_temporal_status(done, TODAY) == deadline.TRANSITION_SETTLED
    assert deadline.work_item_temporal_status(cancelled, TODAY) == deadline.TRANSITION_SETTLED


def test_overdue_work_items_mixes_classes_oldest_first():
    items = [
        {"id": "a1", "deadline": "2026-08-08", "status": "todo"},           # overdue
        {"id": "m1", "target_date": "2026-08-05", "status": "in_progress"},  # overdue (legacy field)
        {"id": "t1", "deadline": "2026-08-09", "status": "todo"},           # due today
        {"id": "done", "deadline": "2026-07-01", "status": "done"},         # settled → excluded
        {"id": "future", "deadline": "2026-08-20", "status": "todo"},       # scheduled → excluded
    ]
    pairs = deadline.overdue_work_items(items, TODAY)
    ids = [it["id"] for it, _ in pairs]
    # oldest 締切 first: m1(08-05) → a1(08-08) → t1(08-09). done/future は出ない。
    assert ids == ["m1", "a1", "t1"]
    statuses = [st for _, st in pairs]
    assert statuses == [
        deadline.TRANSITION_OVERDUE, deadline.TRANSITION_OVERDUE, deadline.TRANSITION_DUE]


# --- 後方互換: sales_entities の re-export が同一オブジェクトであること -------

def test_sales_entities_reexports_are_identity():
    assert se.temporal_status is deadline.temporal_status
    assert se.scan_overdue is deadline.scan_overdue
    assert se.TRANSITION_OVERDUE == deadline.TRANSITION_OVERDUE
    assert se.TRANSITION_SETTLED == deadline.TRANSITION_SETTLED


# --- reminder dedup (サーバ発リマインダの二重配信防止, e-4953) ----------------

def test_pending_reminders_excludes_already_reminded():
    items = [{"id": "t1", "deadline": "2026-08-07", "status": "todo"}]
    assert [i["id"] for i, _ in deadline.pending_reminders(items, TODAY)] == ["t1"]
    deadline.mark_reminded(items[0])
    assert items[0][deadline.REMINDED_FOR_KEY] == "2026-08-07"
    # 同じ締切値では再送しない。
    assert deadline.pending_reminders(items, TODAY) == []


def test_pending_reminders_refires_when_deadline_changes():
    # 締切を延ばして再び過ぎたら値が変わるので再通知される。
    items = [{"id": "t1", "deadline": "2026-08-07", "status": "todo"}]
    deadline.mark_reminded(items[0])
    assert deadline.pending_reminders(items, TODAY) == []
    items[0]["deadline"] = "2026-08-08"  # 延長したが今日より前 → 再び overdue
    assert [i["id"] for i, _ in deadline.pending_reminders(items, TODAY)] == ["t1"]


def test_pending_reminders_excludes_terminal_and_future():
    items = [
        {"id": "done", "deadline": "2026-08-01", "status": "done"},
        {"id": "future", "deadline": "2026-08-20", "status": "todo"},
    ]
    assert deadline.pending_reminders(items, TODAY) == []
