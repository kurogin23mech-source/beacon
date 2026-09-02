"""decision の who.agent が認証 claims から解決されることの検証 (ms-166 e-5997)。

背景: CLI 発の decision fire 経路 (task-done / completion-verdict / 手動 record /
pr-intent / review 採否 / scope 承認) は、認証 claims に email があるのに ``who`` へ
載せておらず、``who.agent`` が全 fire 経路で一律 None になっていた (回帰)。dm-send だけは
envelope の sender email を agent に渡していたので値が入っていた。sales-dashboards
セッションからの実データ観測 (2026-09-02) で発覚。

構造修正: 解決規則を ``decision_event.agent_from_claims`` の 1 関数に集約し、各 fire 経路が
必ずこれを通す (= 1 箇所忘れるだけで再発する直書きを排除)。

契約:
  * human claims (email あり) → agent = email。
  * machine key / email 無し / claims 無し → agent = None (backend は agent 無しが正)。
  * builder (task_done / completion_verdict / scope_approval) は渡された agent を
    who.agent に載せる。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import decision_event as de  # noqa: E402


# --- agent_from_claims: 単一真実源のロジック ------------------------------

def test_agent_from_human_claims_is_email():
    assert de.agent_from_claims({"sub": "100953", "email": "a@b.com"}) == "a@b.com"


def test_agent_from_machine_or_empty_is_none():
    # machine key は email を持たない (audit_email="") → agent None
    assert de.agent_from_claims({"sub": "machine-x", "email": ""}) is None
    assert de.agent_from_claims({"sub": "only-sub"}) is None
    assert de.agent_from_claims(None) is None


def test_agent_strips_whitespace_only_to_none():
    assert de.agent_from_claims({"email": "   "}) is None
    assert de.agent_from_claims({"email": "  x@y.z  "}) == "x@y.z"


# --- builder が agent を who.agent に載せる -------------------------------

def test_task_done_carries_agent_into_who():
    ev = de.decision_event_from_task_done(
        entry_id="e-1", decider_user_id="100953", agent="a@b.com")
    assert ev["who"]["agent"] == "a@b.com"
    assert ev["who"]["user_id"] == "100953"


def test_completion_verdict_carries_agent_into_who():
    ev = de.decision_event_from_completion_verdict(
        target_id="ms-2", verdict="done", decider_user_id="100953",
        agent="a@b.com")
    assert ev["who"]["agent"] == "a@b.com"


def test_scope_approval_carries_agent_into_who():
    ev = de.decision_event_from_scope_approval(
        decision="approve", decider_user_id="100953", agent="a@b.com")
    assert ev["who"]["agent"] == "a@b.com"


def test_generic_build_carries_agent_into_who():
    ev = de.build_decision_event(
        kind="review-adjudication", decision="ax 3件採用",
        decided_by="autonomous-AI", evidence=["review:ax"],
        who={"user_id": "100953", "session_id": "sv-1",
             "agent": de.agent_from_claims({"email": "a@b.com"})})
    assert ev["who"]["agent"] == "a@b.com"


def test_no_agent_still_normalizes_to_none_not_keyerror():
    # 回帰前の挙動 (agent 未指定) でも壊れない: None に正規化される
    ev = de.decision_event_from_task_done(entry_id="e-1", decider_user_id="1")
    assert ev["who"]["agent"] is None
