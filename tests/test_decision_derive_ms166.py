"""成果物 (PR intent) からの decision 導出 (ms-166 e-5972)。

判断軌跡を、別途 `beacon decision record` を叩かせず、既に書かれた成果物 (PR intent)
から DERIVE する。deliverable = log の導出物にした先例 (ms-161) と同じ発想。

pure な導出ロジック (build / iter / covered) をここで固定する。write-through と
backfill の I/O 配線は cmd_pr / cmd_decision が担う (best_effort でラップ)。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import decision_derive as dd  # noqa: E402


# --- build_pr_intent_decision ----------------------------------------------

def test_intentから決定payloadを組む():
    p = dd.build_pr_intent_decision(
        703, "fix(ms-166): 採否を溶接", "採否を構造で捕獲する", decided_by="autonomous-AI")
    assert p["kind"] == "pr-intent"
    assert p["decision"] == "fix(ms-166): 採否を溶接"   # what = 変更 (title)
    assert p["rationale"] == "採否を構造で捕獲する"       # why = intent
    assert p["decided_by"] == "autonomous-AI"
    assert p["evidence"] == ["pr:703"]                   # PR link は evidence 実 link


def test_intentが無ければ導出しない():
    # 「なぜ」を捏造しない: intent 空の PR は decision を生まない (evidence 空と同原則)。
    assert dd.build_pr_intent_decision(703, "t", "", decided_by="x") is None
    assert dd.build_pr_intent_decision(703, "t", "   ", decided_by="x") is None


def test_title無しはPR番号にfallback():
    p = dd.build_pr_intent_decision(703, "", "なぜ", decided_by="x")
    assert p["decision"] == "PR#703"


# --- iter_pr_intent_artifacts ----------------------------------------------

def _project():
    return {"milestones": [
        {"id": "ms-1", "entries": [
            {"type": "pr", "description": "PR A", "meta": {"pr_number": 701, "intent": "why A"}},
            {"type": "pr", "description": "PR B", "meta": {"pr_number": 702, "intent": ""}},   # intent 無 → 除外
            {"type": "commit", "description": "c", "meta": {}},                                # 非 PR → 除外
        ]},
        {"id": "ms-2", "entries": [
            {"type": "pr", "description": "PR C", "meta": {"pr_number": 703, "intent": "why C"}},
        ]},
    ]}


def test_intentを持つPRだけを列挙():
    got = list(dd.iter_pr_intent_artifacts(_project()))
    assert got == [(701, "PR A", "why A"), (703, "PR C", "why C")]


def test_空プロジェクトでも落ちない():
    assert list(dd.iter_pr_intent_artifacts({})) == []
    assert list(dd.iter_pr_intent_artifacts({"milestones": []})) == []


# --- covered_pr_numbers (dedup, 冪等) ---------------------------------------

def test_既存pr_intent_decisionのPR番号を拾う():
    existing = [
        {"kind": "pr-intent", "evidence": ["pr:701"]},
        {"kind": "pr-intent", "evidence": ["pr:703", "review:x"]},
        {"kind": "dm-send", "evidence": ["pr:999"]},   # 別 kind → 対象外
        {"kind": "pr-intent", "evidence": []},          # evidence 無 → 何も足さない
    ]
    assert dd.covered_pr_numbers(existing) == {"701", "703"}


def test_covered_空入力():
    assert dd.covered_pr_numbers([]) == set()
    assert dd.covered_pr_numbers(None) == set()


# --- backfill command: dry-run 既定 / --apply / dedup -----------------------

import cmd_decision       # noqa: E402
import commands_shared    # noqa: E402
import cmd_pr             # noqa: E402


class _FakeClient:
    def __init__(self, existing):
        self.existing = existing
        self.posted = []

    def list_decisions(self, pid, kind="", limit=100):
        return {"decisions": [d for d in self.existing if d.get("kind") == kind]}

    def record_decision(self, pid, payload):
        self.posted.append(payload)
        return {"decision_id": "dec-x", "kind": payload["kind"]}


def _wire(monkeypatch, existing):
    fake = _FakeClient(existing)
    monkeypatch.setattr(cmd_decision, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(cmd_decision, "_get_api_client",
                        lambda: (fake, {"project_id": "p1"}))
    monkeypatch.setattr(commands_shared, "load_project", lambda: _project())
    monkeypatch.setattr(cmd_pr, "_session_kind_is_human", lambda: False)
    return fake


def test_derive_dry_run既定は書かない(monkeypatch, capsys):
    fake = _wire(monkeypatch, existing=[])
    monkeypatch.delenv("BEACON_APPLY", raising=False)
    monkeypatch.setenv("BEACON_JSON", "1")
    cmd_decision.cmd_decision_derive()
    import json as _j
    out = _j.loads(capsys.readouterr().out)
    assert out == {"apply": False, "derived": 0, "pending": 2,
                   "already_covered": 0, "pr_intent_artifacts": 2}
    assert fake.posted == []                     # dry-run は書かない


def test_derive_applyで導出しdedupする(monkeypatch, capsys):
    # pr:701 は既に arm 上 → skip、pr:703 のみ導出。
    fake = _wire(monkeypatch, existing=[{"kind": "pr-intent", "evidence": ["pr:701"]}])
    monkeypatch.setenv("BEACON_APPLY", "1")
    monkeypatch.setenv("BEACON_JSON", "1")
    cmd_decision.cmd_decision_derive()
    import json as _j
    out = _j.loads(capsys.readouterr().out)
    assert out["derived"] == 1 and out["already_covered"] == 1
    assert [p["evidence"] for p in fake.posted] == [["pr:703"]]
    assert fake.posted[0]["kind"] == "pr-intent"
    assert fake.posted[0]["decided_by"] == "autonomous-AI"
