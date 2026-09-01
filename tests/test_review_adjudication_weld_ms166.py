"""レビュー採否 (finding-level adjudication) の decision seam 溶接 (ms-166 e-5971)。

`beacon pr approve/reject` は PR 単位の verdict を decision arm に溶接済だが、
`/beacon-review-run` で findings を 1 件ずつ採否する瞬間 (= 会話上の採否・撤回) は
decision に落ちていなかった。この溶接は `beacon review done` — レビュー gate を
解消する choke point — に採否を相乗りさせ、「レビューを記録する call が採否を記録
する call」になる (= 別途 AI が自発 record を叩く経路に頼らない)。

真値源の共有: 失敗ハンドリングは commands_shared.best_effort_decision_write に集約
(完遂 verdict と同じ contract)。ここでは「何を書くか (payload)」と「空なら書かない」
「malformed JSON を握って gate は解消」を固定する。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands          # noqa: E402
import commands_shared   # noqa: E402


class _FakeClient:
    def __init__(self):
        self.posted = []

    def record_decision(self, project_id, decision):
        self.posted.append((project_id, decision))
        return {"decision_id": "dec-adj", "kind": decision["kind"]}


def _cloud(monkeypatch, fake, *, human=False):
    monkeypatch.setattr(commands_shared, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(commands_shared, "_get_api_client",
                        lambda: (fake, {"project_id": "p1"}))
    monkeypatch.setattr(commands_shared, "_session_kind_is_human", lambda: human)


# --- decision payload -------------------------------------------------------

def test_採否をdecisionに溶接する(monkeypatch):
    fake = _FakeClient()
    _cloud(monkeypatch, fake, human=False)
    adj = [
        {"finding": "M1", "disposition": "accepted", "rationale": "parity guard 拡張"},
        {"finding": "AX-2", "disposition": "declined", "rationale": "open-vocab 設計に反する"},
    ]
    commands._record_review_adjudication_decision("ax", "702", "", adj)
    assert len(fake.posted) == 1
    _pid, rec = fake.posted[0]
    assert rec["kind"] == "review-adjudication"
    assert rec["decision"] == "ax: 1 accepted / 1 declined"   # verdict 無 → 件数合成
    assert "accepted[M1]: parity guard 拡張" in rec["rationale"]
    assert "declined[AX-2]: open-vocab 設計に反する" in rec["rationale"]
    assert rec["decided_by"] == "autonomous-AI"               # AI session
    assert rec["evidence"] == ["review:ax"]
    assert rec["related"] == {"pr_number": "702", "review_type": "ax"}


def test_明示verdictがdecisionになる(monkeypatch):
    fake = _FakeClient()
    _cloud(monkeypatch, fake, human=True)
    commands._record_review_adjudication_decision(
        "maintainability", "702", "5件採用・1件却下", [])
    rec = fake.posted[0][1]
    assert rec["decision"] == "5件採用・1件却下"
    assert rec["decided_by"] == "human-delegated"             # human session


def test_verdictもadjudicationも無ければ書かない(monkeypatch):
    fake = _FakeClient()
    _cloud(monkeypatch, fake)
    commands._record_review_adjudication_decision("ax", "702", "", [])
    assert fake.posted == []


def test_local_modeでは書かない(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(commands_shared, "_is_cloud_mode", lambda: False)
    monkeypatch.setattr(commands_shared, "_get_api_client",
                        lambda: (fake, {"project_id": "p1"}))
    commands._record_review_adjudication_decision("ax", "702", "v", [{"disposition": "accepted"}])
    assert fake.posted == []


def test_write失敗はwarnして飲む(monkeypatch, caplog):
    class _Raise:
        def record_decision(self, *a, **k):
            raise RuntimeError("502 down")
    _cloud(monkeypatch, _Raise())
    import logging
    with caplog.at_level(logging.WARNING):
        commands._record_review_adjudication_decision("ax", "702", "v", [])
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "decision write failed" in msgs and "review-adjudication" in msgs and "702" in msgs


# --- env parsing ------------------------------------------------------------

def test_adjudications_env_good(monkeypatch):
    monkeypatch.setenv("BEACON_REVIEW_ADJUDICATIONS",
                       '[{"finding":"M1","disposition":"accepted"}]')
    out = commands._adjudications_from_env()
    assert out == [{"finding": "M1", "disposition": "accepted"}]


def test_adjudications_env_malformed_は空(monkeypatch, capsys):
    monkeypatch.setenv("BEACON_REVIEW_ADJUDICATIONS", "{not json")
    out = commands._adjudications_from_env()
    assert out == []
    assert "JSON 解析に失敗" in capsys.readouterr().err   # gate は解消・記録だけ skip


def test_adjudications_env_非list_は空(monkeypatch):
    monkeypatch.setenv("BEACON_REVIEW_ADJUDICATIONS", '{"finding":"x"}')
    assert commands._adjudications_from_env() == []


def test_adjudications_env_未設定_は空(monkeypatch):
    monkeypatch.delenv("BEACON_REVIEW_ADJUDICATIONS", raising=False)
    assert commands._adjudications_from_env() == []
