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

def test_kind_vocabulary_is_closed():
    # 経路が増えたらここを直す forcing function。silent 拡張を禁止する。
    assert de.DECISION_KINDS == frozenset(
        {"dm-send", "trek-review", "scope-approval", "halt", "resume"}
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
    assert set(e) == {
        "decision_id", "kind", "decision", "context",
        "rationale", "who", "related", "created_at",
    }
    assert "outcome" not in e
    assert e["decision_id"].startswith("dec-")
    assert e["who"] == {"session_id": "sv-1", "user_id": "u1", "agent": "claude"}
    assert e["related"] == {
        "event_id": "evt-1", "trek_id": None,
        "task_id": None, "in_reply_to": "evt-0",
    }
    assert e["created_at"].endswith("Z")


def test_unknown_kind_rejected():
    with pytest.raises(ValueError):
        de.build_decision_event(kind="deploy", decision="shipped")


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
