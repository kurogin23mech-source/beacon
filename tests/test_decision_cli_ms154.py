"""ms-154 e-5594 — `beacon decision record` CLI verb (log-time backstop の記録口)。

log-time backstop が「動く」ことの CLI 側検証: 環境変数インターフェース経由で
決定を組み立て、cloud mode では server の decision 書き込み口へ正しい payload を
post し、local mode では no-op、不正入力 (what / evidence 欠落) は非ゼロ終了。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import cmd_decision  # noqa: E402


class _FakeClient:
    def __init__(self):
        self.posted = []

    def record_decision(self, project_id, decision):
        self.posted.append((project_id, decision))
        return {"decision_id": "dec-42", "kind": decision["kind"]}


def _set_env(monkeypatch, **kw):
    for k in ("BEACON_DECISION_WHAT", "BEACON_DECISION_KIND",
              "BEACON_DECISION_RATIONALE", "BEACON_DECISION_DECIDED_BY",
              "BEACON_DECISION_EVIDENCE", "BEACON_DECISION_RELATED_TASK",
              "BEACON_JSON"):
        monkeypatch.delenv(k, raising=False)
    for k, v in kw.items():
        monkeypatch.setenv(k, v)


def test_split_evidence_drops_blanks():
    assert cmd_decision._split_evidence("a\n\n  \nb") == ["a", "b"]
    assert cmd_decision._split_evidence("") == []


def test_record_requires_what(monkeypatch):
    _set_env(monkeypatch, BEACON_DECISION_EVIDENCE="commit:abc")
    with pytest.raises(SystemExit) as e:
        cmd_decision.cmd_decision_record()
    assert e.value.code == 1


def test_record_requires_evidence(monkeypatch):
    _set_env(monkeypatch, BEACON_DECISION_WHAT="chose X")
    with pytest.raises(SystemExit) as e:
        cmd_decision.cmd_decision_record()
    assert e.value.code == 1


def test_record_rejects_bad_decided_by(monkeypatch):
    _set_env(monkeypatch, BEACON_DECISION_WHAT="x",
             BEACON_DECISION_EVIDENCE="commit:abc",
             BEACON_DECISION_DECIDED_BY="the-vibes")
    with pytest.raises(SystemExit) as e:
        cmd_decision.cmd_decision_record()
    assert e.value.code == 1


def test_record_local_mode_is_noop(monkeypatch, capsys):
    _set_env(monkeypatch, BEACON_DECISION_WHAT="x",
             BEACON_DECISION_EVIDENCE="commit:abc")
    monkeypatch.setattr(cmd_decision, "_is_cloud_mode", lambda: False)
    cmd_decision.cmd_decision_record()  # no raise, no post
    assert "local mode" in capsys.readouterr().out


def test_record_posts_to_cloud(monkeypatch):
    fake = _FakeClient()
    _set_env(monkeypatch, BEACON_DECISION_WHAT="chose the additive schema",
             BEACON_DECISION_KIND="log-backstop",
             BEACON_DECISION_RATIONALE="new-field would break callers",
             BEACON_DECISION_EVIDENCE="server/decision_event.py:75\ncommit:b2a3927",
             BEACON_DECISION_RELATED_TASK="e-5591")
    monkeypatch.setattr(cmd_decision, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(cmd_decision, "_get_api_client",
                        lambda: (fake, {"project_id": "p1"}))
    cmd_decision.cmd_decision_record()
    assert len(fake.posted) == 1
    pid, rec = fake.posted[0]
    assert pid == "p1"
    assert rec["kind"] == "log-backstop"
    assert rec["decision"] == "chose the additive schema"
    assert rec["decided_by"] == "autonomous-AI"  # default
    assert rec["rationale"] == "new-field would break callers"
    assert rec["evidence"] == ["server/decision_event.py:75", "commit:b2a3927"]
    assert rec["related"] == {"task_id": "e-5591"}


def test_record_defaults_kind_to_log_backstop(monkeypatch):
    fake = _FakeClient()
    _set_env(monkeypatch, BEACON_DECISION_WHAT="x",
             BEACON_DECISION_EVIDENCE="commit:abc")
    monkeypatch.setattr(cmd_decision, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(cmd_decision, "_get_api_client",
                        lambda: (fake, {"project_id": "p1"}))
    cmd_decision.cmd_decision_record()
    assert fake.posted[0][1]["kind"] == "log-backstop"
