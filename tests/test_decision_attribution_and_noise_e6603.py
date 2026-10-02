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


# ---------------------------------------------------------------------------
# 4. 独立レビュー採否で足した挙動 (ax A-1/A-2/A-3/A-4, 保守性 M-1/M-2/M-3)
# ---------------------------------------------------------------------------

def test_and_composes_kind_with_exclude_kinds(capsys=None):
    """ax A-4: 両方渡したら AND 合成し、片方が黙って勝つ優先規則を置かない。

    除外集合から ``kind`` 自身を引くので「kind=dm-send を明示したら dm-send が引ける」
    性質は保たれる。旧実装は elif で ``kind`` 指定時に exclude_kinds を丸ごと無視しており、
    署名からは「両方渡すと何が起きるか」が読めなかった。
    """
    rows = [_row("dm-send", "2026-09-01T00:00:00Z", "d1"),
            _row("task-done", "2026-09-02T00:00:00Z", "t1"),
            _row("log-backstop", "2026-09-03T00:00:00Z", "l1")]
    # kind 明示 + その kind が除外集合に居る → kind 自身は引かれるので引ける
    out = de.window_decision_events(rows, kind="dm-send",
                                    exclude_kinds=de.NON_DECISION_KINDS)
    assert [r["decision_id"] for r in out] == ["d1"]
    # kind 明示 + 別 kind の除外 → 素直な AND (dm-send は kind 絞りで既に消えている)
    out = de.window_decision_events(rows, kind="task-done",
                                    exclude_kinds=frozenset({"log-backstop"}))
    assert [r["decision_id"] for r in out] == ["t1"]
    # kind 明示 + 自分自身を除外 → kind を引くので空にならない (消えない保証)
    out = de.window_decision_events(rows, kind="task-done",
                                    exclude_kinds=frozenset({"task-done"}))
    assert [r["decision_id"] for r in out] == ["t1"]


def test_route_computes_the_exclusion_set_once():
    """保守性 M-1: フィルタ用と開示用で `not kind` を 2 回評価しない。

    2 回書くと片方だけ直したとき「除外していないと表示しつつ実際は除外する」(逆も)
    食い違いが起き、開示が実態と一致する保証が手作業に落ちる。
    """
    import ast
    path = os.path.join(_SERVER, "routers_projects.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "list_decisions":
            fn = node
    assert fn is not None
    src = ast.get_source_segment(open(path, encoding="utf-8").read(), fn) or ""
    # NON_DECISION_KINDS の参照は 1 箇所だけ (= 集合の算出が 1 回)
    assert src.count("NON_DECISION_KINDS") == 1, (
        "除外集合が 2 回算出されている — フィルタと開示が drift しうる")


def test_record_json_carries_the_derived_attribution_and_target():
    """ax A-2: --json でも機械が決めた帰属 / 対象を出す。

    --json は自動化経路が「何が記録されたか」を確認する正規手段。人間向け print にだけ
    開示を実装すると、この修正が足した情報そのものが機械の読み手から消える。
    """
    import importlib
    import json as _json
    import cmd_decision
    importlib.reload(cmd_decision)

    class _Client:
        def record_decision(self, pid, payload):
            return {"decision_id": "dec-1", "kind": payload.get("kind")}

    env = {
        "BEACON_DECISION_WHAT": "opp-3 の成約を決めた",
        "BEACON_DECISION_EVIDENCE": "commit:abc1234",
        "BEACON_DECISION_RATIONALE": "",
        "BEACON_DECISION_DECIDED_BY": "",
        "BEACON_DECISION_RELATED_TASK": "",
        "BEACON_DECISION_RELATED_TARGET": "",
        "BEACON_DECISION_KIND": "",
        "BEACON_JSON": "1",
    }
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    import io
    import contextlib
    buf = io.StringIO()
    try:
        cmd_decision._is_cloud_mode = lambda: True
        cmd_decision._get_api_client = lambda: (_Client(), {"project_id": "p"})
        with contextlib.redirect_stdout(buf):
            cmd_decision.cmd_decision_record()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    out = _json.loads(buf.getvalue())
    assert out["decision_id"] == "dec-1"
    assert out["decided_by_source"] == "session-kind"
    assert out["target_id"] == "opp-3"
    assert out["target_id_source"] == "text"


def test_explicit_target_flag_beats_the_text_inference():
    """ax A-3: 明示指定 > 本文からの導出。曖昧を構造的に解消する経路。"""
    captured = {}
    _run_record({"BEACON_DECISION_WHAT": "ms-166 と ms-160 を直した",
                 "BEACON_DECISION_RELATED_TARGET": "ms-166"}, captured)
    assert captured["related"]["target_id"] == "ms-166"


def test_both_cli_fronts_expose_related_target_for_record():
    # CLI フロントは 2 つ (bash の bin/beacon と python の beacon_cli/dispatch.py)。
    # 片方だけに旗を足すと、その経路だけ曖昧を解消できない。
    root = os.path.join(os.path.dirname(__file__), "..")
    for rel in ("bin/beacon", "beacon_cli/dispatch.py"):
        body = open(os.path.join(root, rel), encoding="utf-8").read()
        assert "--related-target" in body, f"{rel} に --related-target が無い"
        assert "BEACON_DECISION_RELATED_TARGET" in body, (
            f"{rel} が BEACON_DECISION_RELATED_TARGET を渡していない")


def test_target_regex_is_built_at_import_time():
    """保守性 M-3: 家の流儀 (deliverable_map._WEDGE_TAG_RE) と同じ即時構築。

    遅延 global キャッシュは「最初の呼び出し時点の prefix 表で固定される」stale
    キャッシュの失敗モードを新設する (既存コードには無い)。
    """
    import re as _re
    assert isinstance(dd._TARGET_REF_RE, _re.Pattern), (
        "正規表現が import 時に構築されていない (遅延 None センチネルは lib の前例外)")


def test_list_discloses_the_exclusion_even_when_everything_was_excluded():
    """ax A-1: **0 件のときこそ開示が要る**。

    旧実装は `if not rows: print("(決定なし)"); return` が開示より手前にあり、
    「全部が除外されて 0 件」と「そもそも判断記録が無い」が同じ文言に潰れていた。
    前者を後者と読むと「このプロジェクトには判断記録が無い」と誤って結論する。
    実データでは通信ログが流れの大半を占める時期があるので十分起こりうる。
    """
    import contextlib
    import importlib
    import io
    import cmd_decision
    importlib.reload(cmd_decision)

    class _Client:
        def list_decisions(self, pid, **kw):
            # 全件除外されて 0 件、ただし「何を外したか」は返ってくる
            return {"decisions": [], "count": 0, "excluded_kinds": ["dm-send"]}

    env = {"BEACON_DECISION_KIND": "", "BEACON_DECISION_LIMIT": "",
           "BEACON_DECISION_SESSION": "", "BEACON_DECISION_TARGET": "",
           "BEACON_JSON": ""}
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    buf = io.StringIO()
    try:
        cmd_decision._is_cloud_mode = lambda: True
        cmd_decision._get_api_client = lambda: (_Client(), {"project_id": "p"})
        with contextlib.redirect_stdout(buf):
            cmd_decision.cmd_decision_list()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    out = buf.getvalue()
    assert "(決定なし)" in out
    assert "dm-send" in out and "--kind dm-send" in out, (
        "0 件のときに除外の開示が落ちている — 『除外で 0 件』と『記録が無い』が"
        "同じ文言に潰れる")
