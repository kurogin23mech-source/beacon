"""ms-166 e-6603 — 判断記録の帰属・対象を機械決定し、通信ログを既定 read から外す。

## なぜ要るか (実データ)

`beacon decision list --limit 100` を実測した結果:

  kind                 件数   target 解決率
  completion-verdict     34    34/34
  review-adjudication    31     0/31
  log-backstop           16     **0/16**
  disposition            12    12/12
  task-done               3     0/3
  dm-send                 3     0/3  (decided_by も None)
  pr-intent               1     0/1

3 つの欠陥が見えた:

1. **log-backstop が対象を持たない** (16/16 で空)。判断記録が「どの対象の話か」を
   持たないと `decision list --target` に一件も載らず、記録はあるのに辿れない。
   本文には実際に対象 id が書かれている (「opp-3 の成約を…」) のに拾っていなかった。
2. **帰属が固定文字列** だった。`cmd_decision_record` の既定が "autonomous-AI" の
   ハードコードで、人間端末から打った判断まで「人間未確認の AI 単独決定」として残る
   (= 帰属が逆。最も監査が要る値を取り違える)。session-kind から導く単一真実源
   `commands_shared.decided_by_for_review` が既に在るのに使っていなかった。
3. **dm-send が既定 read を埋める**。decided_by も target も None の通信ログで、
   判断ではない。session-start の「最近の決定」が送信ログで埋まり、本物の判断が
   読めない (= ms-166 が塞ぎたい silent 非機能そのもの)。

## このファイルが pin するもの

- 本文からの対象解決の境界 — 何を拾い、何を拾わず、曖昧なら **推測しない**。
- 帰属が session-kind から導出され、明示指定が勝つこと。
- dm-send の既定除外が **limit の前** に効くこと (後で絞ると判断がこぼれる)。
- 既定除外が **開示される** こと (黙って狭めない) と、`kind` 明示で従来どおり引けること。
- 3 保存先すべてが `exclude_kinds` を受けること (1 つ落ちると backend 切替で復活する)。
"""
from __future__ import annotations

import importlib
import inspect
import os
import sys

import pytest

_SERVER = os.path.join(os.path.dirname(__file__), "..", "server")
sys.path.insert(0, _SERVER)

import decision_event as de  # noqa: E402
import decision_derive as dd  # noqa: E402  (lib/ は conftest が path に載せる)


# ---------------------------------------------------------------------------
# 1. 本文からの対象解決 (欠陥 1)
# ---------------------------------------------------------------------------

def test_resolves_a_single_target_from_the_text():
    assert dd.resolve_target_from_text("opp-3 の成約を決めた") == "opp-3"
    assert dd.resolve_target_from_text("ms-166 の掃討で e-6602 を直した",
                                       "根拠は commit abc") == "ms-166"


def test_ambiguous_text_resolves_to_nothing():
    # 複数の対象に触れた判断を片方に帰属させると、監査で「この対象はこう判断された」と
    # 誤読される。空で返して明示を促すのが正しい (= 黙って一方に寄せない)。
    assert dd.resolve_target_from_text("ms-166 と ms-160 の両方") == ""
    assert dd.target_ids_in_text("ms-166 と ms-160 の両方") == ["ms-166", "ms-160"]


def test_entry_ids_are_not_targets():
    # e- はタスク / エントリで target prefix ではない。task_id として別に運ぶ。
    assert dd.resolve_target_from_text("e-6602 だけ書いた") == ""


def test_prefix_table_is_the_source_not_a_hardcoded_list():
    # 新しい target クラスが台帳 (work_model の prefix 表) に載った瞬間に解決が効く
    # こと。ここをハードコードすると、クラス追加のたびに silent に取りこぼす。
    import work_model as wm
    for prefix in wm.known_target_prefixes():
        tid = f"{prefix}9"
        assert dd.resolve_target_from_text(f"{tid} の件") == tid, prefix


def test_op_does_not_mis_match_inside_opp():
    # op- は opp- の接頭辞だが、リテラルに - を含むので opp-3 を op- として拾わない。
    assert dd.target_ids_in_text("opp-3 と op-1") == ["opp-3", "op-1"]


def test_does_not_slice_target_ids_out_of_other_words():
    assert dd.target_ids_in_text("transforms-7 や xms-1 や aop-2") == []


# ---------------------------------------------------------------------------
# 2. 帰属の機械決定 (欠陥 2)
# ---------------------------------------------------------------------------

def _run_record(env: dict, captured: dict):
    """cmd_decision_record を cloud 偽装で 1 回走らせ、送った payload を captured に残す。"""
    import cmd_decision
    importlib.reload(cmd_decision)

    class _Client:
        def record_decision(self, pid, payload):
            captured.update(payload)
            return {"decision_id": "dec-1", "kind": payload.get("kind")}

    base = {
        "BEACON_DECISION_WHAT": "",
        "BEACON_DECISION_RATIONALE": "",
        "BEACON_DECISION_DECIDED_BY": "",
        "BEACON_DECISION_EVIDENCE": "commit:abc1234",
        "BEACON_DECISION_RELATED_TASK": "",
        "BEACON_DECISION_KIND": "",
        "BEACON_JSON": "",
    }
    base.update(env)
    old = {k: os.environ.get(k) for k in base}
    os.environ.update(base)
    try:
        cmd_decision._is_cloud_mode = lambda: True
        cmd_decision._get_api_client = lambda: (_Client(), {"project_id": "p"})
        cmd_decision.cmd_decision_record()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_attribution_is_derived_from_session_kind_not_hardcoded(monkeypatch):
    import commands_shared as cs
    captured = {}
    # 人間端末 → human-delegated (旧実装は固定文字列 autonomous-AI で帰属が逆だった)
    monkeypatch.setattr(cs, "_session_kind_is_human", lambda: True)
    _run_record({"BEACON_DECISION_WHAT": "決めた"}, captured)
    assert captured["decided_by"] == "human-delegated"

    captured.clear()
    monkeypatch.setattr(cs, "_session_kind_is_human", lambda: False)
    _run_record({"BEACON_DECISION_WHAT": "決めた"}, captured)
    assert captured["decided_by"] == "autonomous-AI"


def test_explicit_decided_by_still_wins(monkeypatch):
    import commands_shared as cs
    monkeypatch.setattr(cs, "_session_kind_is_human", lambda: True)
    captured = {}
    _run_record({"BEACON_DECISION_WHAT": "決めた",
                 "BEACON_DECISION_DECIDED_BY": "AI-proposed-human-chose"}, captured)
    assert captured["decided_by"] == "AI-proposed-human-chose"


def test_attribution_derivation_shares_the_single_source():
    # 同じ写像を 2 つ目のコピーとして書いていないこと (e-6601 保守性 M-1 と同じ規律)。
    import commands_shared as cs
    import cmd_decision
    src = inspect.getsource(cmd_decision.cmd_decision_record)
    assert "decided_by_for_review" in src, (
        "帰属の導出が decided_by_for_review を経由していない — 写像の二重定義は drift する")
    assert hasattr(cs, "decided_by_for_review")


def test_record_attaches_the_resolved_target():
    captured = {}
    _run_record({"BEACON_DECISION_WHAT": "opp-3 の成約を決めた",
                 "BEACON_DECISION_RELATED_TASK": "e-1"}, captured)
    assert captured["related"]["target_id"] == "opp-3"
    assert captured["related"]["task_id"] == "e-1"


def test_record_leaves_target_empty_when_ambiguous():
    captured = {}
    _run_record({"BEACON_DECISION_WHAT": "ms-166 と ms-160 を直した"}, captured)
    assert "target_id" not in (captured.get("related") or {})


# ---------------------------------------------------------------------------
# 3. 通信ログの既定除外 (欠陥 3)
# ---------------------------------------------------------------------------

def _row(kind: str, created_at: str, did: str) -> dict:
    return {"kind": kind, "created_at": created_at, "decision_id": did,
            "decision": "x"}


def test_dm_send_is_declared_a_non_decision_kind():
    assert "dm-send" in de.NON_DECISION_KINDS
    # 本物の判断族は除外対象に入れない (入れると監査が引けなくなる)。
    for k in ("completion-verdict", "review-adjudication", "log-backstop",
              "task-done", "disposition", "pr-intent", "gate-judgement"):
        assert k not in de.NON_DECISION_KINDS, k


def test_exclusion_is_applied_before_the_limit_window():
    # ここが肝。後で絞ると「最新 limit 件の中の残り」になり、除外した分だけ本物の判断が
    # 窓からこぼれる (e-5970 で直した filter-after-truncate と同じ穴の再生産)。
    rows = [_row("dm-send", f"2026-09-0{i}T00:00:00Z", f"d{i}") for i in range(1, 6)]
    rows += [_row("log-backstop", "2026-09-06T00:00:00Z", "keep")]
    out = de.window_decision_events(rows, limit=3,
                                   exclude_kinds=de.NON_DECISION_KINDS)
    assert [r["decision_id"] for r in out] == ["keep"]
    # 除外しなければ dm-send が窓を埋めて本物の判断を押し出す (= 修正前の症状)。
    out_noex = de.window_decision_events(rows, limit=3)
    assert "keep" in [r["decision_id"] for r in out_noex]
    assert len(out_noex) == 3


def test_explicit_kind_still_returns_the_excluded_rows():
    # 消すのではなく既定から外すだけ。データを到達不能にしない。
    rows = [_row("dm-send", "2026-09-01T00:00:00Z", "d1")]
    out = de.window_decision_events(rows, kind="dm-send",
                                   exclude_kinds=de.NON_DECISION_KINDS)
    assert [r["decision_id"] for r in out] == ["d1"]


def test_no_exclude_kinds_keeps_previous_behaviour():
    rows = [_row("dm-send", "2026-09-01T00:00:00Z", "d1")]
    assert len(de.window_decision_events(rows)) == 1


@pytest.mark.parametrize("mod_name", ["dynamodb_client", "firestore_client",
                                      "mysql_client"])
def test_every_backend_accepts_exclude_kinds(mod_name):
    # 1 保存先だけ落ちると、backend を切り替えた本番で通信ログが既定 read に復活する
    # (= 手元緑 / 本番だけ違う silent な穴)。kind を 3 保存先に通した e-5970 と同じ規律。
    try:
        mod = importlib.import_module(mod_name)
    except Exception as exc:  # pragma: no cover - optional deps at import
        pytest.skip(f"{mod_name} import unavailable: {exc}")
    params = inspect.signature(mod.list_decision_events).parameters
    assert "exclude_kinds" in params, (
        f"{mod_name}.list_decision_events が exclude_kinds を受けていない")
    assert params["exclude_kinds"].default is None


def test_dynamo_roundtrip_excludes_dm_send_by_default():
    import dynamodb_client as dyn
    dyn._DECISION_EVENTS_FALLBACK.clear()
    pid = "proj-noise"
    for i, k in enumerate(("dm-send", "log-backstop", "dm-send")):
        dyn.append_decision_event(pid, de.build_decision_event(
            kind=k, decision="x", who={"session_id": "sv", "user_id": "u"},
            created_at=f"2026-09-0{i + 1}T00:00:00.000000Z"))
    default_read = dyn.list_decision_events(
        pid, exclude_kinds=de.NON_DECISION_KINDS)
    assert [r["kind"] for r in default_read] == ["log-backstop"]
    assert len(dyn.list_decision_events(pid, kind="dm-send")) == 2
