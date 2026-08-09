"""session-start の締切超過 surface (scripts/session-start-deadlines.py) — ms-139
e-4952。

3 種類の work item (milestone target_date / task deadline / activity deadline) を
L2 締切規則で overdue 判定し、冪等に表示する glue のテスト。subprocess (_beacon_json)
は stub して、行の組み立て・除外・並び・整形を固める。
"""

import importlib.util
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "lib"))
import deadline  # noqa: E402

# scripts/ はパッケージでないので spec から直接ロードする。
_SPEC = importlib.util.spec_from_file_location(
    "ss_deadlines",
    os.path.join(REPO, "scripts", "session-start-deadlines.py"))
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)

TODAY = "2026-08-09"


def test_format_empty_is_blank():
    assert mod._format([]) == ""


def test_collect_rows_dev_milestone_and_task(monkeypatch):
    status = {
        "profession": "dev",
        "targets": [
            {"id": "ms-1", "kind": "milestone", "status": "in_progress",
             "label": "遅れてるMS", "detail": {"target_date": "2026-08-05"}},
            {"id": "ms-2", "kind": "milestone", "status": "done",
             "label": "完了MS", "detail": {"target_date": "2026-08-01"}},  # done→除外
        ],
    }

    def fake_json(args):
        if args[:2] == ["task", "list"]:
            return {"entries": [
                {"type": "task", "id": "e-1", "description": "期限切れ",
                 "deadline": "2026-08-06", "status": "todo"},
                {"type": "task", "id": "e-2", "description": "未来",
                 "deadline": "2026-08-30", "status": "todo"},       # 未来→除外
                {"type": "task", "id": "e-3", "description": "完了",
                 "deadline": "2026-08-01", "status": "done"},       # done→除外
            ]}
        return None

    monkeypatch.setattr(mod, "_beacon_json", fake_json)
    rows = mod._collect_rows(status, TODAY)
    # oldest first: ms-1(08-05) → e-1(08-06)。done MS と未来/done task は除外。
    assert [(r["kind"], r["label"]) for r in rows] == [
        ("milestone", "遅れてるMS"), ("task", "期限切れ")]
    assert rows[0]["temporal"] == deadline.TRANSITION_OVERDUE


def test_collect_rows_scans_non_inprogress_milestones(monkeypatch):
    # 思想レビュー finding#4: observing/todo/waiting な MS 配下の締切付き task も
    # session-start が拾う(done/cancelled の MS だけ除外)。
    status = {
        "profession": "dev",
        "targets": [
            {"id": "ms-obs", "kind": "milestone", "status": "observing",
             "label": "観察中MS", "detail": {"target_date": ""}},
            {"id": "ms-done", "kind": "milestone", "status": "done",
             "label": "完了MS", "detail": {"target_date": ""}},
        ],
    }
    calls = []

    def fake_json(args):
        calls.append(args)
        if args[:2] == ["task", "list"] and args[-1] == "ms-obs":
            return {"entries": [{"type": "task", "id": "e-1", "description": "観察中の期限切れ",
                                 "deadline": "2026-08-06", "status": "todo"}]}
        return {"entries": []}

    monkeypatch.setattr(mod, "_beacon_json", fake_json)
    rows = mod._collect_rows(status, TODAY)
    assert [(r["kind"], r["label"]) for r in rows] == [("task", "観察中の期限切れ")]
    # done な MS の task list は引かない(terminal 除外)。
    assert ["task", "list", "-m", "ms-done"] not in calls


def test_collect_rows_sales_activity(monkeypatch):
    status = {"profession": "sales", "targets": []}

    def fake_json(args):
        if args[:2] == ["opportunity", "due"]:
            return {"opportunities": [], "activities": [
                {"act_id": "act-1", "description": "8/7会食",
                 "deadline": "2026-08-07", "activity_status": "overdue",
                 "opp_id": "opp-1", "opp_title": "A社商談",
                 "who_has_the_ball": "self"},
            ]}
        return None

    monkeypatch.setattr(mod, "_beacon_json", fake_json)
    rows = mod._collect_rows(status, TODAY)
    assert len(rows) == 1
    assert rows[0]["kind"] == "activity"
    assert "A社商談" in rows[0]["context"]
    out = mod._format(rows)
    assert "締切超過" in out and "8/7会食" in out


def test_collect_rows_dev_skips_sales_source(monkeypatch):
    # dev project では opportunity due を引かない (呼ばれたら記録して検出)。
    status = {"profession": "dev", "targets": []}
    calls = []

    def fake_json(args):
        calls.append(args)
        return None

    monkeypatch.setattr(mod, "_beacon_json", fake_json)
    mod._collect_rows(status, TODAY)
    assert not any(a[:2] == ["opportunity", "due"] for a in calls)
