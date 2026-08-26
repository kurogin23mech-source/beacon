"""ms-154 e-5593 — PR review 採否 (approve / re-work / reject) が decision arm に
記録されることを構造的に検証する。

「掃除機がある≠掃除した」に従い、builder / endpoint の存在だけでなく:
  (A) 配線: cmd_pr の approve / reject / request-changes が
      ``_record_review_decision`` を正しい verdict で呼ぶ。
  (B) recorder: cloud mode で ``_record_review_decision`` が
      ``review-adjudication`` 決定を正しい payload で server 書き込み口へ post する。
を実証する。
"""
from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import cmd_pr  # noqa: E402
import commands_shared  # noqa: E402


def _project_with_pr():
    return {"milestones": [{
        "id": "ms-1", "title": "M", "status": "in_progress",
        "entries": [{
            "id": "e-400", "type": "pr", "description": "PR one",
            "status": "in_review",
            "meta": {"url": "https://github.com/o/r/pull/5",
                     "review_status": "pending"},
        }],
    }]}


def _run_verb(verb_name, *, entry_id="e-400", rationale="LGTM"):
    """Invoke a cmd_pr review verb in-process with load/save stubbed."""
    data = _project_with_pr()
    orig_load, orig_save = cmd_pr.load_project, cmd_pr.save_project
    cmd_pr.load_project = lambda: data
    cmd_pr.save_project = lambda d: None
    env_backup = {k: os.environ.get(k) for k in
                  ("BEACON_ENTRY_ID", "BEACON_RATIONALE", "BEACON_JSON",
                   "BEACON_NO_AUTO_DONE")}
    os.environ["BEACON_ENTRY_ID"] = entry_id
    os.environ["BEACON_RATIONALE"] = rationale
    os.environ["BEACON_JSON"] = "1"
    os.environ["BEACON_NO_AUTO_DONE"] = "1"
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            try:
                getattr(cmd_pr, verb_name)()
            except SystemExit:
                pass
    finally:
        cmd_pr.load_project, cmd_pr.save_project = orig_load, orig_save
        for k, v in env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# (A) 配線: verbs call the recorder with the correct verdict
# ---------------------------------------------------------------------------

def _capture_recorder(monkeypatch):
    calls = []
    monkeypatch.setattr(cmd_pr, "_record_review_decision",
                        lambda entry_id, verdict, rationale:
                        calls.append((entry_id, verdict, rationale)))
    return calls


def test_reject_records_reject_verdict(monkeypatch):
    calls = _capture_recorder(monkeypatch)
    _run_verb("cmd_pr_reject", rationale="AC 未達")
    assert calls == [("e-400", "reject", "AC 未達")]


def test_request_changes_records_rework_verdict(monkeypatch):
    calls = _capture_recorder(monkeypatch)
    _run_verb("cmd_pr_request_changes", rationale="ここを直して")
    assert calls == [("e-400", "re-work", "ここを直して")]


def test_approve_records_approve_verdict(monkeypatch):
    calls = _capture_recorder(monkeypatch)
    # neutralize the independent-review gate + auto-done side-helpers so the
    # test isolates the decision-recording wiring.
    monkeypatch.setattr(cmd_pr, "_review_gate_check", lambda *a, **k: None)
    monkeypatch.setattr(cmd_pr, "_judge_pr_approve_auto_done", lambda *a, **k: [])
    _run_verb("cmd_pr_approve", rationale="良い")
    assert calls == [("e-400", "approve", "良い")]


# ---------------------------------------------------------------------------
# (B) recorder: cloud-mode posts a valid review-adjudication decision
# ---------------------------------------------------------------------------

class _FakeClient:
    def __init__(self):
        self.posted = []

    def record_decision(self, project_id, decision):
        self.posted.append((project_id, decision))
        return {"decision_id": "dec-1", "kind": decision["kind"]}


def test_recorder_posts_review_adjudication_in_cloud(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(commands_shared, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(commands_shared, "_get_api_client",
                        lambda: (fake, {"project_id": "p1"}))
    cmd_pr._record_review_decision("e-400", "approve", "受容理由")
    assert len(fake.posted) == 1
    pid, rec = fake.posted[0]
    assert pid == "p1"
    assert rec["kind"] == "review-adjudication"
    assert rec["decision"] == "approve"
    assert rec["decided_by"] == "autonomous-AI"
    # ms-154 e-5650: no self-reference (pr:e-400) in evidence — it lives in
    # related.task_id. With no real link supplied, evidence is honestly empty.
    assert rec["evidence"] == []
    assert rec["rationale"] == "受容理由"
    assert rec["related"]["task_id"] == "e-400"


def test_recorder_noop_in_local_mode(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(commands_shared, "_is_cloud_mode", lambda: False)
    monkeypatch.setattr(commands_shared, "_get_api_client",
                        lambda: (fake, {"project_id": "p1"}))
    cmd_pr._record_review_decision("e-400", "reject", "x")
    assert fake.posted == []  # local mode = no server decision stream


def test_recorder_swallows_client_failure(monkeypatch):
    # best-effort: a raising client must not propagate (never break the flow).
    def _boom():
        raise SystemExit(1)  # mimics _get_api_client's sys.exit on missing creds
    monkeypatch.setattr(commands_shared, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(commands_shared, "_get_api_client", _boom)
    cmd_pr._record_review_decision("e-400", "approve", "x")  # no raise
