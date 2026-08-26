"""ms-90 / e-3242 — decision-event 統一スキーマ + backend parity。

decision-event は DM 発信 / trek-review / scope 承認 / halt-resume の 4 経路の
「決定」を 1 本の append-only ストリームに束ねる統一形。論理スキーマの確定は
e-3245 の doc OqqO02CUvsQzzDMyhhGf (spec, ms-90)。

このファイルが pin するもの:
- schema builder (server/decision_event.py) の不変条件
  (kind 閉じた語彙 / outcome を持たない / who・related の固定 shape /
   created_at 補完 / rationale 任意 / context 空許容 / decision 必須)。
- 3 backend (dynamodb / mysql / firestore) が同一シグネチャで
  append_decision_event / list_decision_events を公開している (= parity)。
- dynamodb in-memory backend の round-trip (順序 / since / limit / 分離 /
  永続化層の outcome guard)。
"""
from __future__ import annotations

import importlib
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import decision_event as de  # noqa: E402


# ---------------------------------------------------------------------------
# Schema builder invariants
# ---------------------------------------------------------------------------

def test_known_kinds_documented_but_vocabulary_open():
    # ms-154 §設計方針1「語彙開放」: 閉語彙 hard gate を廃止し、KNOWN_* は参照用の
    # 文書化リストに降格。ms-90 の 5 経路 + ms-154 decision arm の捕獲対象を含む。
    assert de.KNOWN_DECISION_KINDS >= frozenset(
        {"dm-send", "trek-review", "scope-approval", "halt", "resume"}
    )
    assert {"task-done", "completion-verdict", "review-adjudication",
            "log-backstop"} <= de.KNOWN_DECISION_KINDS
    # 後方互換 alias が同じ集合を指す (= ms-90 期の import 名を壊さない)。
    assert de.DECISION_KINDS is de.KNOWN_DECISION_KINDS


def test_decided_by_vocabulary():
    # ms-154 AC1: decided_by は 4 語彙の一級 enum。
    assert de.DECIDED_BY == frozenset(
        {"autonomous-AI", "AI-proposed-human-chose",
         "human-delegated", "programmatic"}
    )


def test_build_produces_full_shape_without_outcome():
    e = de.build_decision_event(
        kind="dm-send",
        decision="sent",
        context="問題に直面した",
        who={"session_id": "sv-1", "user_id": "u1", "agent": "claude"},
        rationale="相談した方が速いと判断",
        related={"event_id": "evt-1", "in_reply_to": "evt-0"},
    )
    # ms-154: decided_by / evidence / options が additive に加わった 11 キー shape。
    assert set(e) == {
        "decision_id", "kind", "decision", "context", "rationale",
        "decided_by", "evidence", "options",
        "who", "related", "created_at",
    }
    assert "outcome" not in e
    assert e["decision_id"].startswith("dec-")
    assert e["who"] == {"session_id": "sv-1", "user_id": "u1", "agent": "claude"}
    assert e["related"] == {
        "event_id": "evt-1", "trek_id": None,
        "task_id": None, "target_id": None, "in_reply_to": "evt-0",
    }
    assert e["created_at"].endswith("Z")
    # legacy 経路 (decided_by 未指定) は additive default で通る (= AC6 後方互換)。
    assert e["decided_by"] is None
    assert e["evidence"] == []
    assert e["options"] == []


def test_unknown_kind_accepted_but_empty_rejected():
    # 語彙開放後: 未知 kind は受け付ける (= 職種横断の汎用アーム)。
    e = de.build_decision_event(kind="deploy", decision="shipped")
    assert e["kind"] == "deploy"
    # ただし空 kind は構造的に弾く (= 種別なしの決定は辿れない)。
    with pytest.raises(ValueError):
        de.build_decision_event(kind="", decision="x")
    with pytest.raises(ValueError):
        de.build_decision_event(kind="   ", decision="x")


def test_decided_by_allows_honest_empty_evidence():
    # ms-154 e-5650: the old "decided_by → evidence 非空必須" ValueError is GONE.
    # A decision may carry decided_by with empty evidence — that is the honest
    # audit signal "no physical backing" (phantom done), not a schema violation.
    # The self-reference is never fabricated to satisfy a tautological invariant.
    e0 = de.build_decision_event(
        kind="task-done", decision="e-5591 done",
        decided_by="autonomous-AI",  # evidence 無し → もう拒否しない
    )
    assert e0["decided_by"] == "autonomous-AI"
    assert e0["evidence"] == []  # 捏造せず空のまま = 裏付け無しを露出
    # real evidence を伴えばそのまま積む。
    e = de.build_decision_event(
        kind="task-done", decision="e-5591 done",
        decided_by="autonomous-AI",
        rationale="AC1/AC6 を満たすと判断",
        evidence=["server/decision_event.py:75", "commit:abc1234"],
        options=["schema 新設", "既存 field 再利用"],
    )
    assert e["decided_by"] == "autonomous-AI"
    assert e["evidence"] == ["server/decision_event.py:75", "commit:abc1234"]
    assert e["options"] == ["schema 新設", "既存 field 再利用"]


def test_unknown_decided_by_rejected():
    with pytest.raises(ValueError):
        de.build_decision_event(
            kind="task-done", decision="x",
            decided_by="the-vibes", evidence=["ref"],
        )


def test_evidence_normalizes_single_string_and_drops_blanks():
    e = de.build_decision_event(
        kind="task-done", decision="x",
        decided_by="programmatic", evidence="commit:deadbeef",
    )
    assert e["evidence"] == ["commit:deadbeef"]  # 単一文字列 → list 化
    e2 = de.build_decision_event(
        kind="review-adjudication", decision="approve",
        decided_by="human-delegated",
        evidence=["", "  ", "file:1"],  # 空要素は落ちる
    )
    assert e2["evidence"] == ["file:1"]


def test_decision_required():
    with pytest.raises(ValueError):
        de.build_decision_event(kind="halt", decision="")
    with pytest.raises(ValueError):
        de.build_decision_event(kind="halt", decision="   ")


def test_context_may_be_empty_but_rationale_optional():
    # context は主役だが hard block しない (= 空でも組み立ては通る)。
    e = de.build_decision_event(kind="halt", decision="halt")
    assert e["context"] == ""
    assert e["rationale"] is None  # rationale 任意 → None


def test_who_and_related_normalized_from_partial():
    e = de.build_decision_event(
        kind="trek-review", decision="approve",
        who={"session_id": "sv-9"},  # user_id / agent 省略
        related={"trek_id": "tk-1", "task_id": "T-5"},
    )
    assert e["who"] == {"session_id": "sv-9", "user_id": "", "agent": None}
    assert e["related"]["trek_id"] == "tk-1"
    assert e["related"]["task_id"] == "T-5"
    assert e["related"]["event_id"] is None


def test_created_at_and_decision_id_preserved_when_given():
    e = de.build_decision_event(
        kind="resume", decision="resume",
        created_at="2026-01-01T00:00:00.000000Z", decision_id="dec-fixed",
    )
    assert e["created_at"] == "2026-01-01T00:00:00.000000Z"
    assert e["decision_id"] == "dec-fixed"


def test_assert_no_outcome_guard():
    de.assert_no_outcome({"kind": "dm-send"})  # clean → no raise
    with pytest.raises(ValueError):
        de.assert_no_outcome({"kind": "dm-send", "outcome": "worked"})


# ---------------------------------------------------------------------------
# Backend parity: all 3 clients expose the same signatures
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mod_name", ["dynamodb_client", "firestore_client",
                                      "mysql_client"])
def test_backend_exposes_decision_event_functions(mod_name):
    try:
        mod = importlib.import_module(mod_name)
    except Exception as exc:  # pragma: no cover - optional deps at import
        pytest.skip(f"{mod_name} import unavailable: {exc}")
    assert hasattr(mod, "append_decision_event")
    assert hasattr(mod, "list_decision_events")
    # list signature parity: (project_id, *, limit=100, since="")
    sig = inspect.signature(mod.list_decision_events)
    params = sig.parameters
    assert "limit" in params and params["limit"].default == 100
    assert "since" in params and params["since"].default == ""


# ---------------------------------------------------------------------------
# dynamodb in-memory round-trip (append-only stream semantics)
# ---------------------------------------------------------------------------

def _fresh_dynamo():
    import dynamodb_client as dyn
    dyn._DECISION_EVENTS_FALLBACK.clear()
    return dyn


def test_dynamo_roundtrip_order_since_limit_isolation():
    dyn = _fresh_dynamo()
    pid = "proj-A"
    for i, (k, d, c) in enumerate([
        ("dm-send", "sent", "A"),
        ("trek-review", "approve", "B"),
        ("halt", "halt", "C"),
    ]):
        rec = de.build_decision_event(
            kind=k, decision=d, context=c,
            who={"session_id": f"sv{i}", "user_id": "u"},
            created_at=f"2026-07-11T0{i}:00:00.000000Z",
        )
        dyn.append_decision_event(pid, rec)

    rows = dyn.list_decision_events(pid)
    assert [r["kind"] for r in rows] == ["dm-send", "trek-review", "halt"]

    since = dyn.list_decision_events(pid, since="2026-07-11T00:30:00.000000Z")
    assert [r["kind"] for r in since] == ["trek-review", "halt"]

    assert len(dyn.list_decision_events(pid, limit=1)) == 1
    assert dyn.list_decision_events("proj-B") == []  # project isolation


def test_dynamo_mints_id_and_created_at_when_absent():
    dyn = _fresh_dynamo()
    did = dyn.append_decision_event("proj-C", {"kind": "dm-send",
                                               "decision": "sent"})
    assert did.startswith("dec-")
    row = dyn.list_decision_events("proj-C")[0]
    assert row["created_at"]  # stamped by persistence layer


def test_dynamo_persistence_rejects_outcome():
    dyn = _fresh_dynamo()
    with pytest.raises(ValueError):
        dyn.append_decision_event("proj-D", {"kind": "dm-send",
                                             "decision": "x",
                                             "outcome": "sneaky"})


# ---------------------------------------------------------------------------
# e-3246 — DM 発信の decision-event 組み立て (主役経路)
# ---------------------------------------------------------------------------

def test_dm_send_record_shape():
    e = de.decision_event_from_dm_send(
        sender_session_id="sv-1", sender_user_id="u1",
        context="listener が 60s で死ぬ問題", rationale="相談が速い",
        event_id="evt-9", in_reply_to="evt-0", agent="claude",
    )
    assert e["kind"] == "dm-send"
    assert e["decision"] == "sent"
    assert e["context"] == "listener が 60s で死ぬ問題"
    assert e["rationale"] == "相談が速い"
    assert e["who"] == {"session_id": "sv-1", "user_id": "u1",
                        "agent": "claude"}
    assert e["related"]["event_id"] == "evt-9"
    assert e["related"]["in_reply_to"] == "evt-0"
    assert "outcome" not in e


def test_dm_send_record_context_optional():
    # 背景なしでも組み立てられる (= hard block しない、warning は CLI の責務)。
    e = de.decision_event_from_dm_send(sender_session_id="sv-2", event_id="e1")
    assert e["context"] == ""
    assert e["rationale"] is None


def test_maybe_dm_send_record_only_for_dm_channel():
    # dm 以外は None (= 記録しない)。
    assert de.maybe_dm_send_record(channel="trek-trigger", payload={},
                                   event_id="e1") is None
    assert de.maybe_dm_send_record(channel="operation-trigger", payload={},
                                   event_id="e1") is None
    rec = de.maybe_dm_send_record(channel="dm", payload={}, event_id="e1")
    assert rec is not None and rec["kind"] == "dm-send"


def test_maybe_dm_send_record_extracts_in_reply_to_from_payload():
    rec = de.maybe_dm_send_record(
        channel="dm",
        payload={"recipient_session_id": "sv-x", "in_reply_to": "evt-parent"},
        sender_session_id="sv-1", event_id="evt-child",
    )
    assert rec["related"]["in_reply_to"] == "evt-parent"
    assert rec["related"]["event_id"] == "evt-child"


def test_maybe_dm_send_record_handles_non_dict_payload():
    rec = de.maybe_dm_send_record(channel="dm", payload=None, event_id="e1")
    assert rec["related"]["in_reply_to"] is None


# ---------------------------------------------------------------------------
# e-3247 — 残り 3 経路 (scope 承認 / trek-review / halt-resume) の組み立て
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("decision", ["approve", "deny"])
def test_scope_approval_record(decision):
    e = de.decision_event_from_scope_approval(
        decision=decision, decider_user_id="u9", event_id="evt-7",
        context="権限外の action だった",
    )
    assert e["kind"] == "scope-approval"
    assert e["decision"] == decision
    assert e["who"]["user_id"] == "u9"
    assert e["related"]["event_id"] == "evt-7"
    assert "outcome" not in e


def test_trek_review_decision_from_state_mapping():
    # leader_review からの遷移先 → 判断語 (閉じた mapping)。
    assert de.trek_review_decision_from_state("done") == "approve"
    assert de.trek_review_decision_from_state("user_review") == "forward-to-user"
    assert de.trek_review_decision_from_state("working") == "re-work"
    assert de.trek_review_decision_from_state("todo") == "re-work"


def test_trek_review_record():
    e = de.decision_event_from_trek_review(
        decision="approve", trek_id="tk-1", task_id="T-2",
        decider_session_id="sv-leader", decider_user_id="u1",
        context="AC を満たしていた",
    )
    assert e["kind"] == "trek-review"
    assert e["decision"] == "approve"
    assert e["related"]["trek_id"] == "tk-1"
    assert e["related"]["task_id"] == "T-2"
    assert e["who"]["session_id"] == "sv-leader"


def test_halt_and_resume_records():
    h = de.decision_event_from_halt(
        resumed=False, trek_id="tk-1", issuer_session_id="sv-x",
        context="本番が壊れたので止めた",
    )
    assert h["kind"] == "halt" and h["decision"] == "halt"
    assert h["context"] == "本番が壊れたので止めた"
    assert h["related"]["trek_id"] == "tk-1"

    r = de.decision_event_from_halt(resumed=True, trek_id="tk-1")
    assert r["kind"] == "resume" and r["decision"] == "resume"
    assert r["context"] == ""


def test_rationale_flows_through_every_path_helper():
    # e-3241: 「なぜ」(rationale) が各経路の helper で運べる。
    dm = de.decision_event_from_dm_send(event_id="e", rationale="速い方が良い")
    sc = de.decision_event_from_scope_approval(decision="deny",
                                               rationale="権限外だから")
    tr = de.decision_event_from_trek_review(decision="re-work",
                                            rationale="AC 未達で差し戻し")
    ht = de.decision_event_from_halt(resumed=False, rationale="影響が広い")
    assert dm["rationale"] == "速い方が良い"
    assert sc["rationale"] == "権限外だから"
    assert tr["rationale"] == "AC 未達で差し戻し"
    assert ht["rationale"] == "影響が広い"


# ---------------------------------------------------------------------------
# ms-154 e-5592 — task done 判定 / 目的達成 verdict を decision arm へ昇格
# ---------------------------------------------------------------------------

def test_task_done_record_shape_and_defaults():
    e = de.decision_event_from_task_done(
        entry_id="e-5591",
        done_reason="AC 全達成、30 テスト green と判断",
        evidence=["commit:b2a3927"],
        decider_user_id="u1",
    )
    assert e["kind"] == "task-done"
    assert e["decision"] == "done"            # what = done
    assert e["rationale"] == "AC 全達成、30 テスト green と判断"  # why = done_reason
    assert e["decided_by"] == "autonomous-AI"  # CLI done の保守的 default
    # ms-154 e-5650: evidence は実 link のみ。自己参照 (task:e-5591) は積まない
    # (related.task_id が運ぶ)。渡した commit だけが残る。
    assert e["evidence"] == ["commit:b2a3927"]
    assert e["related"]["task_id"] == "e-5591"
    assert e["who"]["user_id"] == "u1"


def test_task_done_evidence_empty_when_no_commit():
    # ms-154 e-5650: phantom done (= commit 照合が空振り) は evidence 空のまま通す。
    # 自己参照 task:<id> を捏造して非空に見せる旧挙動は廃止 (裏付け無しを隠さない)。
    e = de.decision_event_from_task_done(entry_id="e-999", decided_by="autonomous-AI")
    assert e["evidence"] == []              # 空 = 物理的裏付け無しの監査シグナル
    assert e["related"]["task_id"] == "e-999"  # 自己参照は related が運ぶ
    assert e["rationale"] is None


def test_task_done_decided_by_overridable():
    e = de.decision_event_from_task_done(
        entry_id="e-1", decided_by="AI-proposed-human-chose",
        evidence=["commit:abc"],
    )
    assert e["decided_by"] == "AI-proposed-human-chose"


def test_completion_verdict_record_shape_and_defaults():
    e = de.decision_event_from_completion_verdict(
        target_id="ms-154",
        verdict="done",
        done_reason="全 6 タスク done、目的達成レビュー合格",
        evidence=["commit:deadbee"],
        decider_user_id="u9",
    )
    assert e["kind"] == "completion-verdict"
    assert e["decision"] == "done"
    assert e["rationale"] == "全 6 タスク done、目的達成レビュー合格"
    # milestone 完遂は人間承認が原則なので default は AI-proposed-human-chose。
    assert e["decided_by"] == "AI-proposed-human-chose"
    # ms-154 e-5650: 自己参照 (target:ms-154) は積まず、実 link のみ (related が運ぶ)。
    assert e["evidence"] == ["commit:deadbee"]
    assert e["related"]["target_id"] == "ms-154"
    assert e["related"]["task_id"] is None


def test_completion_verdict_evidence_empty_when_no_link():
    e = de.decision_event_from_completion_verdict(target_id="ms-7")
    assert e["decision"] == "done"                 # verdict 未指定 → done
    assert e["evidence"] == []                     # ms-154 e-5650: 自己参照を積まない
    assert e["related"]["target_id"] == "ms-7"     # 自己参照は related が運ぶ
    assert e["decided_by"] == "AI-proposed-human-chose"


def test_task_done_and_completion_verdict_persist_roundtrip():
    dyn = _fresh_dynamo()
    pid = "proj-arm"
    td = de.decision_event_from_task_done(entry_id="e-1", evidence=["commit:aaa"])
    td["created_at"] = "2026-08-25T01:00:00.000000Z"
    dyn.append_decision_event(pid, td)
    cv = de.decision_event_from_completion_verdict(
        target_id="ms-1", evidence=["commit:bbb"])
    cv["created_at"] = "2026-08-25T02:00:00.000000Z"
    dyn.append_decision_event(pid, cv)
    rows = dyn.list_decision_events(pid)
    assert [r["kind"] for r in rows] == ["task-done", "completion-verdict"]
    assert rows[0]["decided_by"] == "autonomous-AI"
    assert rows[1]["related"]["target_id"] == "ms-1"


def test_all_four_kinds_reachable_via_helpers():
    # 4 経路すべてが helper 経由で閉じた語彙のどれかを出す (= AC4 の構造確認)。
    kinds = {
        de.decision_event_from_dm_send(event_id="e")["kind"],
        de.decision_event_from_scope_approval(decision="approve")["kind"],
        de.decision_event_from_trek_review(decision="approve")["kind"],
        de.decision_event_from_halt(resumed=False)["kind"],
        de.decision_event_from_halt(resumed=True)["kind"],
    }
    assert kinds == {"dm-send", "scope-approval", "trek-review", "halt",
                     "resume"}
    assert kinds <= de.DECISION_KINDS
