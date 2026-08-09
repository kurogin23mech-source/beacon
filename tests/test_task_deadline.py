"""Task の締切 (deadline) フィールド — ms-139 e-4949.

開発 task にも締切を持たせ、L2 締切エンジン (lib/deadline.py) が milestone /
activity と同じ規則で overdue 判定できることを検証する。CLI (bash bin/beacon +
python dispatch) → env → core の配線は E2E で確認済みだが、ここでは core 層の
永続化・JSON 露出・L2 統合を pure に固める。
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import core  # noqa: E402
import deadline  # noqa: E402


def _project_with_ms():
    return {
        "name": "test",
        "milestones": [{
            "id": "ms-1", "title": "MS", "status": "in_progress",
            "progress": 0, "target_date": "", "entries": [], "commits": [],
        }],
    }


def test_task_add_stores_deadline():
    data = _project_with_ms()
    eid = core.task_add(data, "ms-1", "会食フォロー", deadline="2026-08-07",
                        allow_untriaged=True)
    entry = core.find_entry(data, eid)[2]
    assert entry["deadline"] == "2026-08-07"


def test_task_add_without_deadline_omits_field():
    # 締切は任意。渡さなければフィールドを持たない (エントリを膨らませない)。
    data = _project_with_ms()
    eid = core.task_add(data, "ms-1", "締切なし", allow_untriaged=True)
    entry = core.find_entry(data, eid)[2]
    assert "deadline" not in entry


def test_task_update_sets_and_changes_deadline():
    data = _project_with_ms()
    eid = core.task_add(data, "ms-1", "打診", allow_untriaged=True)
    # 後から締切を付与。
    core.task_update(data, eid, deadline="2026-08-20")
    assert core.find_entry(data, eid)[2]["deadline"] == "2026-08-20"
    # 後追いで変更。
    core.task_update(data, eid, deadline="2026-08-05")
    assert core.find_entry(data, eid)[2]["deadline"] == "2026-08-05"


def test_task_update_empty_deadline_is_no_change():
    # 空文字は「変更なし」(他フィールドと同じ規約)。既存の締切を消さない。
    data = _project_with_ms()
    eid = core.task_add(data, "ms-1", "打診", deadline="2026-08-20",
                        allow_untriaged=True)
    core.task_update(data, eid, description="打診(改)")  # deadline 省略
    entry = core.find_entry(data, eid)[2]
    assert entry["deadline"] == "2026-08-20"
    assert entry["description"] == "打診(改)"


def test_entries_to_json_exposes_deadline():
    data = _project_with_ms()
    core.task_add(data, "ms-1", "会食", deadline="2026-08-07", allow_untriaged=True)
    ms = data["milestones"][0]
    js = core.entries_to_json(ms["entries"])
    assert js[0]["deadline"] == "2026-08-07"


def test_l2_engine_flags_overdue_task():
    # core で作った task を L2 締切エンジンがそのまま overdue 判定できる
    # (task/milestone/activity 共通規則)。
    data = _project_with_ms()
    core.task_add(data, "ms-1", "超過", deadline="2026-08-07", allow_untriaged=True)
    core.task_add(data, "ms-1", "未来", deadline="2026-08-20", allow_untriaged=True)
    tasks = core.entries_to_json(data["milestones"][0]["entries"])
    overdue = deadline.overdue_work_items(tasks, "2026-08-09")
    assert [it["description"] for it, _ in overdue] == ["超過"]
