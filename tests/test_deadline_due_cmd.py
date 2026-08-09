"""CLI-layer tests for ``beacon deadline due`` (ms-142 e-5010).

``commands.cmd_deadline_due`` is the single occupation-agnostic deadline-review
verb: it consumes ``occupation.iter_deadline_candidates`` (the same enumeration
the server reminder walks) and applies the L2 temporal rule. These tests pin,
against a temp project (BEACON_PROJECT_FILE + BEACON_JSON):

  * dev: an overdue milestone (target_date) and its overdue task surface; a
    future task and a done task do not.
  * a work item under a terminal (done) Target is dropped — the session-start
    display parity the script used to enforce itself (ms-139 finding#4).
  * sales: an overdue activity surfaces with no profession branch.
  * rows are oldest-deadline first.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

LIB = Path(__file__).parent.parent / "lib"
sys.path.insert(0, str(LIB))

import commands  # noqa: E402


def _project(tmp_path, monkeypatch, data: dict):
    cwd = tmp_path / "proj"
    (cwd / ".beacon").mkdir(parents=True)
    (cwd / ".beacon" / "project.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("BEACON_PROJECT_FILE",
                       str(cwd / ".beacon" / "project.json"))
    monkeypatch.setenv("BEACON_JSON", "1")


def _run(capsys) -> list:
    commands.cmd_deadline_due()
    return json.loads(capsys.readouterr().out)["items"]


TODAY_ENV = "2026-08-09"  # deadline compares plain YYYY-MM-DD strings


def _dev_data():
    return {
        "name": "d", "profession": "dev",
        "milestones": [
            {"id": "ms-1", "title": "遅れてるMS", "status": "in_progress",
             "target_date": "2026-08-05",
             "entries": [
                 {"id": "e-1", "type": "task", "description": "期限切れ",
                  "deadline": "2026-08-06", "status": "todo"},
                 {"id": "e-2", "type": "task", "description": "未来",
                  "deadline": "2026-08-30", "status": "todo"},
                 {"id": "e-3", "type": "task", "description": "完了",
                  "deadline": "2026-08-01", "status": "done"},
                 {"id": "e-9", "type": "commit", "description": "済 commit"},
             ]},
        ],
    }


def test_dev_milestone_and_task(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(commands, "_today_iso", lambda: TODAY_ENV)
    _project(tmp_path, monkeypatch, _dev_data())
    items = _run(capsys)
    # oldest first: ms-1(08-05) then e-1(08-06); future/done task + commit excluded.
    assert [(r["kind"], r["label"]) for r in items] == [
        ("milestone", "遅れてるMS"), ("task", "期限切れ")]


def test_task_under_done_milestone_excluded(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(commands, "_today_iso", lambda: TODAY_ENV)
    data = _dev_data()
    data["milestones"][0]["status"] = "done"   # terminal parent Target
    _project(tmp_path, monkeypatch, data)
    items = _run(capsys)
    # A done milestone is itself excluded (temporal rule) AND its still-todo
    # overdue task is dropped (target_status terminal) — nothing surfaces.
    assert items == []


def test_task_under_observing_milestone_still_surfaces(tmp_path, monkeypatch, capsys):
    # observing/todo/waiting are NOT terminal — their overdue tasks still show
    # (ms-139 finding#4: the net stays wide, only done/cancelled are dropped).
    monkeypatch.setattr(commands, "_today_iso", lambda: TODAY_ENV)
    data = _dev_data()
    data["milestones"][0]["status"] = "observing"
    data["milestones"][0]["target_date"] = ""   # no target-level deadline
    _project(tmp_path, monkeypatch, data)
    items = _run(capsys)
    assert [(r["kind"], r["label"]) for r in items] == [("task", "期限切れ")]


def test_sales_activity(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(commands, "_today_iso", lambda: TODAY_ENV)
    data = {
        "name": "s", "profession": "sales", "milestones": [],
        "opportunities": [
            {"id": "opp-1", "label": "A社商談", "status": "open", "phase": "lead",
             "activities": [
                 {"id": "act-1", "description": "8/7会食", "deadline": "2026-08-07",
                  "status": "todo"},
             ]},
        ],
    }
    _project(tmp_path, monkeypatch, data)
    items = _run(capsys)
    assert len(items) == 1
    assert items[0]["kind"] == "activity"
    assert items[0]["label"] == "8/7会食"
    assert "opp-1" in items[0]["context"]
