"""Parity proof for the ms-142 e-5010 deadline-enumeration付け替え (leader 握り).

'既存 deadline テストが緑' は parity の必要条件だが十分条件ではない — 既存テストが
filter の全 edge を覆う保証がない。so this pins parity DIRECTLY: on a rich MIXED
fixture (done/observing/todo/in_progress milestones with deadline/no-deadline tasks
+ opportunities with activities + a milestone target_date), the SET of firing
tuples ``(id, kind, deadline_value, recipient)`` produced by the OLD hand-written
server enumeration must EQUAL the set produced by the NEW iterator-based one.

This is the CLAUDE.md debugging principle in its parity form: prove '旧経路と新経路が
同じ集合を出す', not merely 'a test passed'. The OLD algorithm is inlined here
(``_old_server_candidates`` / ``_old_sessionstart_rows``) as the reference oracle;
the NEW path is the shipped code (``app._deadline_reminder_candidates`` /
``commands.cmd_deadline_due``).

Documented filter difference the unification PRESERVES (leader 握り: 差異を顕在化
して倒し方を明示): the OLD server included work items under terminal (done/cancelled)
Targets; the OLD session-start display EXCLUDED them. The new code keeps BOTH
behaviors — the enumerator stays pure (server parity) and the ``deadline due`` verb
applies the terminal-Target exclusion (session-start parity). Both parity sets below
therefore hold with NO behavior change.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

LIB = Path(__file__).parent.parent / "lib"
SERVER = Path(__file__).parent.parent / "server"
sys.path.insert(0, str(LIB))
sys.path.insert(0, str(SERVER))

import deadline  # noqa: E402
import commands  # noqa: E402
import app  # noqa: E402

TODAY = "2026-08-09"


def _mixed_project():
    """done/observing/todo/in_progress milestones (deadline/no-deadline tasks,
    done/todo tasks, commits) + opportunities (overdue/future activities,
    communications) + claimed/unclaimed targets."""
    return {
        "id": "p1", "name": "mix", "profession": "dev",
        "milestones": [
            {"id": "ms-1", "title": "進行中", "status": "in_progress",
             "target_date": "2026-08-05",           # overdue target
             "occupation": {"session_id": "sv-ip"},
             "entries": [
                 {"id": "e-1", "type": "task", "description": "期限切れ",
                  "deadline": "2026-08-06", "status": "todo"},        # fires
                 {"id": "e-2", "type": "task", "description": "未来",
                  "deadline": "2099-01-01", "status": "todo"},        # no
                 {"id": "e-3", "type": "task", "description": "完了",
                  "deadline": "2026-08-01", "status": "done"},        # no (done)
                 {"id": "e-4", "type": "task", "description": "締切なし",
                  "status": "todo"},                                  # no (no dl)
                 {"id": "e-5", "type": "commit", "description": "c"},  # not a task
             ]},
            {"id": "ms-2", "title": "観察中", "status": "observing",
             "target_date": "",
             "occupation": {"session_id": "sv-obs"},
             "entries": [
                 {"id": "e-6", "type": "task", "description": "観察中の期限切れ",
                  "deadline": "2026-08-04", "status": "todo"},        # fires
             ]},
            {"id": "ms-3", "title": "完了MS", "status": "done",
             "target_date": "2026-07-01",           # overdue but MS terminal
             "occupation": {"session_id": "sv-done"},
             "entries": [
                 {"id": "e-7", "type": "task", "description": "完了MS配下の期限切れ",
                  "deadline": "2026-08-02", "status": "todo"},  # server:fires / ss:excl
             ]},
            {"id": "ms-4", "title": "未claim", "status": "todo",
             "target_date": "2026-08-03",           # overdue, no claimer
             "entries": []},
        ],
        "opportunities": [
            {"id": "opp-1", "label": "A社", "status": "open", "phase": "lead",
             "occupation": {"session_id": "sv-sales"},
             "activities": [
                 {"id": "act-1", "description": "会食", "deadline": "2026-08-07",
                  "status": "todo"},                                  # fires
                 {"id": "act-2", "description": "未来訪問", "deadline": "2099-01-01",
                  "status": "todo"},                                  # no
             ],
             "communications": [{"id": "comm-1", "description": "deck"}]},
        ],
    }


def _fires(item):
    return deadline.work_item_temporal_status(item, TODAY) in (
        deadline.TRANSITION_DUE, deadline.TRANSITION_OVERDUE)


# --- OLD reference oracles (the hand-written enumerations e-5010 replaced) -----

def _old_server_candidates(project):
    """The OLD server ``_deadline_reminder_candidates`` verbatim (pre-e-5010)."""
    for ms in (project.get("milestones", []) or []):
        recipient = (ms.get("occupation") or {}).get("session_id", "") or ""
        label = ms.get("title") or ms.get("label") or ms.get("id", "")
        yield ms, "milestone", label, recipient
        for e in (ms.get("entries", []) or []):
            if e.get("type") == "task":
                yield e, "task", e.get("description") or e.get("id", ""), recipient
    for opp in (project.get("opportunities", []) or []):
        recipient = (opp.get("occupation") or {}).get("session_id", "") or ""
        for a in (opp.get("activities", []) or []):
            yield a, "activity", a.get("description") or a.get("id", ""), recipient


def _old_sessionstart_rows(project):
    """The OLD session-start ``_collect_rows`` behavior (pre-e-5010): milestone
    target_date + tasks under NON-terminal milestones + sales activities."""
    _TERMINAL = {"done", "cancelled"}
    rows = []
    for ms in project.get("milestones", []):
        item = {"deadline": ms.get("target_date", ""), "status": ms.get("status", "")}
        if _fires(item):
            rows.append(("milestone", ms["id"], ms.get("target_date", "")))
    for ms in project.get("milestones", []):
        if ms.get("status") in _TERMINAL:
            continue
        for e in ms.get("entries", []):
            if e.get("type") == "task" and _fires(e):
                rows.append(("task", e["id"], deadline.deadline_of(e)))
    for opp in project.get("opportunities", []):
        for a in opp.get("activities", []):
            if _fires(a):
                rows.append(("activity", a["id"], deadline.deadline_of(a)))
    return set(rows)


# --- Parity: SERVER site -------------------------------------------------------

def test_server_firing_set_parity():
    project = _mixed_project()

    def firing_set(candidates):
        return {
            (item.get("id"), kind, deadline.deadline_of(item), recipient)
            for item, kind, _label, recipient in candidates
            if _fires(item)
        }

    old = firing_set(_old_server_candidates(project))
    new = firing_set(app._deadline_reminder_candidates(project))
    assert new == old, f"server parity broke: only-new={new - old}, only-old={old - new}"
    # sanity: the fixture actually exercises firing rows (not a vacuous ==).
    assert ("e-1", "task", "2026-08-06", "sv-ip") in new
    assert ("e-7", "task", "2026-08-02", "sv-done") in new   # terminal-MS task included
    assert ("act-1", "activity", "2026-08-07", "sv-sales") in new
    assert ("ms-1", "milestone", "2026-08-05", "sv-ip") in new


# --- Parity: SESSION-START site (via the new `deadline due` verb) --------------

def test_sessionstart_row_parity(tmp_path, monkeypatch, capsys):
    project = _mixed_project()
    old_rows = _old_sessionstart_rows(project)

    cwd = tmp_path / "proj"
    (cwd / ".beacon").mkdir(parents=True)
    (cwd / ".beacon" / "project.json").write_text(
        json.dumps(project, ensure_ascii=False), encoding="utf-8")
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(cwd / ".beacon" / "project.json"))
    monkeypatch.setenv("BEACON_JSON", "1")
    monkeypatch.setattr(commands, "_today_iso", lambda: TODAY)

    commands.cmd_deadline_due()
    items = json.loads(capsys.readouterr().out)["items"]
    # map the verb's rows into the same (kind, id, deadline) shape. context is
    # "<target_id>" for a target, "<target_id> / <item_id>" for a work item.
    new_rows = set()
    for r in items:
        ctx = r["context"]
        rid = ctx.split(" / ")[-1]
        new_rows.add((r["kind"], rid, r["deadline"]))
    assert new_rows == old_rows, (
        f"session-start parity broke: only-new={new_rows - old_rows}, "
        f"only-old={old_rows - new_rows}")
    # sanity: terminal-MS task e-7 is EXCLUDED here (session-start display rule),
    # while the server parity test above INCLUDED it — the documented, preserved
    # difference between the two sites.
    assert ("task", "e-7", "2026-08-02") not in new_rows
    assert ("task", "e-6", "2026-08-04") in new_rows     # observing MS task kept
