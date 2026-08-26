"""ms-154 e-5651 — target-review decisions land on the decision arm (CLI path).

Two capture面 that the CLI applies via a whole-project PUT (so they never reach a
server route that would record them):

- completion verdict (target approve): the attainment verdict done/observing/closed,
  carrying the transition-approval's RICH rationale — implementer intent + the
  independent judge's review_evidence + real evidence refs (AC2 「昇格」 + AC3
  observing/closed capture).
- disposition (findings採否): the judge/human's determination of what happened to an
  unstarted important backlog task (AC3).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import cmd_target  # noqa: E402
import commands_shared  # noqa: E402


class _FakeClient:
    def __init__(self):
        self.posted = []

    def record_decision(self, project_id, decision):
        self.posted.append((project_id, decision))
        return {"decision_id": "dec-t", "kind": decision["kind"]}


def _cloud(monkeypatch, fake, *, human=False):
    monkeypatch.setattr(commands_shared, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(commands_shared, "_get_api_client",
                        lambda: (fake, {"project_id": "p1"}))
    monkeypatch.setattr(cmd_target, "_session_kind_is_human", lambda: human)


# --- completion verdict (target approve) ------------------------------------

def _approval_entry():
    return {"id": "e-app", "meta": {
        "target_id": "ms-9",
        "intent": "全 AC 達成、テスト green と判断",
        "evidence": ["commit:abc1234", "doc:spec-9"],
        "review_evidence": [
            {"verdict": "attained", "summary": "AC を原典に照合し充足",
             "source": "judge:fable", "actor": "ai", "at": "2026-08-26"},
        ],
    }}


def test_completion_verdict_records_rich_rationale(monkeypatch):
    fake = _FakeClient()
    _cloud(monkeypatch, fake, human=False)
    cmd_target._record_completion_verdict_decision(
        "ms-9", "done", _approval_entry(), "レビュー合格につき承認")
    assert len(fake.posted) == 1
    _pid, rec = fake.posted[0]
    assert rec["kind"] == "completion-verdict"
    assert rec["decision"] == "done"
    # rich rationale weaves approval reason + implementer intent + judge verdict.
    assert "承認: レビュー合格につき承認" in rec["rationale"]
    assert "主張(intent): 全 AC 達成" in rec["rationale"]
    assert "judge:fable" in rec["rationale"]
    # evidence = real links only (implementer refs + judge provenance), no self-ref.
    assert rec["evidence"] == ["commit:abc1234", "doc:spec-9", "review:judge:fable"]
    assert "target:ms-9" not in rec["evidence"]
    assert rec["related"]["target_id"] == "ms-9"
    # AI-assisted session where the human confirmed the assembled case.
    assert rec["decided_by"] == "AI-proposed-human-chose"


def test_completion_verdict_captures_observing(monkeypatch):
    # AC3: observing/closed verdicts are captured too, not just done.
    fake = _FakeClient()
    _cloud(monkeypatch, fake, human=True)
    cmd_target._record_completion_verdict_decision(
        "ms-9", "observing", _approval_entry(), "観察期間に入る")
    rec = fake.posted[0][1]
    assert rec["decision"] == "observing"
    assert rec["decided_by"] == "human-delegated"  # straight human session


def test_completion_verdict_noop_local(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(commands_shared, "_is_cloud_mode", lambda: False)
    monkeypatch.setattr(commands_shared, "_get_api_client",
                        lambda: (fake, {"project_id": "p1"}))
    cmd_target._record_completion_verdict_decision("ms-9", "done", _approval_entry(), "x")
    assert fake.posted == []


def test_completion_verdict_best_effort_swallows(monkeypatch):
    def _boom():
        raise SystemExit(1)
    monkeypatch.setattr(commands_shared, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(commands_shared, "_get_api_client", _boom)
    monkeypatch.setattr(cmd_target, "_session_kind_is_human", lambda: False)
    cmd_target._record_completion_verdict_decision("ms-9", "done", _approval_entry(), "x")


# --- disposition (findings採否) ---------------------------------------------

def test_disposition_records_adjudication(monkeypatch):
    fake = _FakeClient()
    _cloud(monkeypatch, fake, human=False)
    cmd_target._record_disposition_decision(
        "ms-9", "e-77", "superseded", "SPEC 改訂で不要化", "judge:fable")
    rec = fake.posted[0][1]
    assert rec["kind"] == "disposition"
    assert rec["decision"] == "superseded"
    assert rec["rationale"] == "SPEC 改訂で不要化"
    assert rec["evidence"] == ["source:judge:fable"]
    assert rec["related"] == {"task_id": "e-77", "target_id": "ms-9"}
    assert rec["decided_by"] == "AI-proposed-human-chose"


def test_disposition_empty_evidence_when_no_source(monkeypatch):
    fake = _FakeClient()
    _cloud(monkeypatch, fake, human=True)
    cmd_target._record_disposition_decision("ms-9", "e-77", "done", "実装済", "")
    rec = fake.posted[0][1]
    assert rec["evidence"] == []                 # honest empty, no fabrication
    assert rec["decided_by"] == "human-delegated"
