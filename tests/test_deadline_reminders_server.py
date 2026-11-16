"""サーバ tick の締切超過リマインダ (_fire_due_deadlines) — ms-139 e-4953.

締切を過ぎた work item を検知し、claim 者のセッションへ 1 回だけ DM する。db は
stub して、配信内容・宛先解決・二重配信防止・claim なし skip・done/未来 除外を固める。
実デプロイでの配信 (Cloud Scheduler → tick → bus → 受信 AII idle-wake) は別途 user 検証。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import app  # noqa: E402
import deadline  # noqa: E402

NOW = "2026-08-09T00:00:00Z"


def _sink(monkeypatch):
    """Stub db.append_bus_event and return the list it captures."""
    events = []
    monkeypatch.setattr(app.db, "append_bus_event",
                        lambda pid, bus_data: events.append(bus_data))
    return events


def _fire(monkeypatch, project, report=None):
    """``_fire_due_deadlines`` を 1 回走らせ、印を保存した回数を返す
    (ms-185 e-7066 で契約が ``(pid, now_iso, report)`` に変わり、source が自分で
    読み書きするようになった。以前の戻り値 ``changed`` —— 「呼び出し側が保存
    すべきか」 —— に対応する観測は「保存が走ったか」なのでそれを数える)。

    この足場は **v1 (whole-doc) の store** を模す。行分割された store での
    振る舞いは tests/test_tick_deadline_split_schema_e7066.py が測る。
    """
    import deadline_marks
    saved = []

    def _save_marks(pid, marks, now_iso):
        n = deadline_marks.stamp_whole_doc(project, marks, now_iso)
        if n:
            saved.append(pid)
        return n

    monkeypatch.setattr(app.db, "get_project_deadline_scope",
                        lambda pid, *a, **k: project, raising=False)
    monkeypatch.setattr(app.db, "save_deadline_reminder_marks",
                        _save_marks, raising=False)
    app._fire_due_deadlines("p1", NOW, [] if report is None else report)
    return len(saved)


def _project_with_claimed_ms(**task):
    return {
        "id": "p1",
        "milestones": [{
            "id": "ms-1", "title": "遅れMS", "status": "in_progress",
            "target_date": "",
            "occupation": {"session_id": "sv-owner"},
            "entries": [dict({"id": "e-1", "type": "task",
                              "description": "期限切れ", "status": "todo"}, **task)],
        }],
    }


def test_overdue_task_dms_claimer_once(monkeypatch):
    events = _sink(monkeypatch)
    project = _project_with_claimed_ms(deadline="2026-08-06")
    report = []
    changed = _fire(monkeypatch, project, report)
    assert changed == 1
    assert len(events) == 1
    ev = events[0]
    assert ev["channel"] == "dm"
    assert ev["delivery"] == "propose-to-ai"
    assert ev["payload"]["recipient_session_id"] == "sv-owner"
    assert ev["payload"]["kind"] == "deadline-reminder"
    assert "2026-08-06" in ev["payload"]["message"]
    # dedup 印が work item に刻まれる。
    task = project["milestones"][0]["entries"][0]
    assert task[deadline.REMINDED_FOR_KEY] == "2026-08-06"

    # 2 回目の tick では再送しない。
    report2 = []
    assert _fire(monkeypatch, project, report2) == 0
    assert len(events) == 1


def test_overdue_milestone_target_date_dms_claimer(monkeypatch):
    events = _sink(monkeypatch)
    project = {
        "id": "p1",
        "milestones": [{
            "id": "ms-1", "title": "遅れMS", "status": "in_progress",
            "target_date": "2026-08-05",
            "occupation": {"session_id": "sv-owner"}, "entries": [],
        }],
    }
    changed = _fire(monkeypatch, project)
    assert changed == 1
    assert events[0]["payload"]["work_kind"] == "milestone"
    assert events[0]["payload"]["recipient_session_id"] == "sv-owner"


def test_overdue_activity_dms_opportunity_claimer(monkeypatch):
    events = _sink(monkeypatch)
    project = {
        "id": "p1", "milestones": [],
        "opportunities": [{
            "id": "opp-1", "occupation": {"session_id": "sv-sales"},
            "activities": [{"id": "act-1", "description": "8/7会食",
                            "deadline": "2026-08-07", "status": "todo"}],
        }],
    }
    assert _fire(monkeypatch, project) == 1
    assert events[0]["payload"]["work_kind"] == "activity"
    assert events[0]["payload"]["recipient_session_id"] == "sv-sales"


def test_unclaimed_item_is_skipped_not_dmd(monkeypatch):
    events = _sink(monkeypatch)
    project = {
        "id": "p1",
        "milestones": [{
            "id": "ms-1", "title": "無主MS", "status": "in_progress",
            "target_date": "2026-08-05", "occupation": {}, "entries": [],
        }],
    }
    report = []
    changed = _fire(monkeypatch, project, report)
    assert changed == 0
    assert events == []
    assert report and report[0]["skipped"] == "no claimer session"


def test_done_and_future_not_reminded(monkeypatch):
    events = _sink(monkeypatch)
    project = {
        "id": "p1",
        "milestones": [{
            "id": "ms-1", "title": "MS", "status": "in_progress", "target_date": "",
            "occupation": {"session_id": "sv-owner"},
            "entries": [
                {"id": "e-1", "type": "task", "description": "完了",
                 "deadline": "2026-08-01", "status": "done"},
                {"id": "e-2", "type": "task", "description": "未来",
                 "deadline": "2026-08-30", "status": "todo"},
            ],
        }],
    }
    assert _fire(monkeypatch, project) == 0
    assert events == []


def test_due_today_message_does_not_claim_overdue(monkeypatch):
    # 思想レビュー finding#3: 本日締切(DUE, 未超過)でも発火するが、文面は「過ぎて
    # います」と断言しない(受信 AI の判断入力を汚さない)。
    events = _sink(monkeypatch)
    project = _project_with_claimed_ms(deadline="2026-08-09")  # == NOW の日付
    assert _fire(monkeypatch, project) == 1
    msg = events[0]["payload"]["message"]
    assert "本日" in msg
    assert "過ぎています" not in msg
    assert events[0]["payload"]["temporal"] == deadline.TRANSITION_DUE


def test_refire_after_deadline_extended_then_lapsed(monkeypatch):
    events = _sink(monkeypatch)
    project = _project_with_claimed_ms(deadline="2026-08-06")
    _fire(monkeypatch, project)
    assert len(events) == 1
    # 締切を延ばしたが今日より前 → 再び overdue、値が変わったので再通知。
    project["milestones"][0]["entries"][0]["deadline"] = "2026-08-07"
    assert _fire(monkeypatch, project) == 1
    assert len(events) == 2
