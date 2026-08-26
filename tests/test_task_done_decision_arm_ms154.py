"""ms-154 e-5650 — `beacon task done` (CLI) records a task-done decision.

The audit gap this closes: the CLI marks a task done locally and syncs via a
whole-document PUT, so it never reaches the server ``done_entry`` route where the
decision-arm recording lives. The most audit-critical decision (the AI's own done
judgment) was therefore captured for Web-UI dones only, not for the CLI path that
/beacon-log and /beacon-task actually drive. ``_record_task_done_decision`` posts
it on the primary path, capturing decided_by + real evidence honestly.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import cmd_task  # noqa: E402
import commands_shared  # noqa: E402


class _FakeClient:
    def __init__(self):
        self.posted = []

    def record_decision(self, project_id, decision):
        self.posted.append((project_id, decision))
        return {"decision_id": "dec-9", "kind": decision["kind"]}


def _clear_env(monkeypatch):
    for k in ("BEACON_DECIDED_BY", "BEACON_DONE_EVIDENCE"):
        monkeypatch.delenv(k, raising=False)


def test_records_task_done_decision_in_cloud(monkeypatch):
    fake = _FakeClient()
    _clear_env(monkeypatch)
    monkeypatch.setattr(commands_shared, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(commands_shared, "_get_api_client",
                        lambda: (fake, {"project_id": "p1"}))
    cmd_task._record_task_done_decision("e-1", "AC 全達成と判断")
    assert len(fake.posted) == 1
    pid, rec = fake.posted[0]
    assert pid == "p1"
    assert rec["kind"] == "task-done"
    assert rec["decision"] == "done"
    assert rec["rationale"] == "AC 全達成と判断"
    assert rec["decided_by"] == "autonomous-AI"     # conservative default
    assert rec["evidence"] == []                     # honest empty (no link given)
    assert rec["related"]["task_id"] == "e-1"


def test_captures_decided_by_and_evidence_from_env(monkeypatch):
    # ms-154 e-5650: decided_by is really captured (human-directed vs autonomous
    # become distinguishable), and evidence carries real commit links, not a
    # self-reference.
    fake = _FakeClient()
    monkeypatch.setenv("BEACON_DECIDED_BY", "human-delegated")
    monkeypatch.setenv("BEACON_DONE_EVIDENCE", "commit:abc1234\ncommit:def5678")
    monkeypatch.setattr(commands_shared, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(commands_shared, "_get_api_client",
                        lambda: (fake, {"project_id": "p1"}))
    cmd_task._record_task_done_decision("e-2", "user がクローズを指示")
    rec = fake.posted[0][1]
    assert rec["decided_by"] == "human-delegated"
    assert rec["evidence"] == ["commit:abc1234", "commit:def5678"]


def test_noop_in_local_mode(monkeypatch):
    fake = _FakeClient()
    _clear_env(monkeypatch)
    monkeypatch.setattr(commands_shared, "_is_cloud_mode", lambda: False)
    monkeypatch.setattr(commands_shared, "_get_api_client",
                        lambda: (fake, {"project_id": "p1"}))
    cmd_task._record_task_done_decision("e-1", "x")
    assert fake.posted == []  # local mode = no server decision stream


def test_best_effort_swallows_client_failure(monkeypatch):
    # a raising client (e.g. _get_api_client sys.exit on missing creds) must not
    # propagate — decision recording is a flag, never a gate on `task done`.
    _clear_env(monkeypatch)

    def _boom():
        raise SystemExit(1)

    monkeypatch.setattr(commands_shared, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(commands_shared, "_get_api_client", _boom)
    cmd_task._record_task_done_decision("e-1", "x")  # no raise
