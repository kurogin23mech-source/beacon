"""ms-166 e-6602 — 完遂 decision の冪等 reject (同じ target×verdict を二度残さない)。

## なぜ要るか

完遂 (= target が終端に到達した) を宣言できる入口は 7 経路ある:

  A ``beacon milestone done`` / ``target close``   lib/cmd_target.py
  B ``beacon opportunity judge terminal``          lib/commands.py
  C ``beacon opportunity phase <terminal>``        lib/commands.py  (e-6601 で追加)
  D ``beacon operation close``                     lib/cmd_operation.py
  E ``beacon acquisition status <terminal>``       lib/cmd_acquisition.py
  F ``beacon target approve`` (review gate)        lib/cmd_target.py
  G server の done_milestone route                 server/routers_projects.py

A〜G はすべて ``append_decision_event`` へ収束するが、そこに冪等制約が無く
decision_id を毎回新規 mint して無条件 append していた。対になる deliverable 側
(``deliverable_capture.capture_target_completion``) は「同じ target×category の
active 行が在れば append しない」= first-write-wins を既に持っており、decision 側
だけが非対称に開いていた。

鍵に ``decision`` (= verdict) を含めるのは、1 つの target が ``observing`` →
``done`` と段階的に完遂しうるから。``(target, kind)`` だけを鍵にすると後段の正当な
状態変化が落ち、F の rich rationale を持つ記録まで失われる。

## このファイルが pin するもの

1. 規則 (``decision_event.completion_dedup_key`` /
   ``find_duplicate_completion``) の境界 — 何を重複と見なし、何を見なさないか。
2. store の挙動 — 二度目は append されず既存 ``decision_id`` が返る (dynamodb
   in-memory backend で round-trip)。
3. 3 backend すべてが規則を **呼んでいる** こと (AST 構造抽出)。1 backend だけ
   落ちると「backend を切り替えた時だけ重複が復活する」silent な穴になる。
4. 3 の guard が実際に drift で赤くなること (= test-the-test)。
"""
from __future__ import annotations

import ast
import importlib
import os
import sys
import textwrap

import pytest

_SERVER = os.path.join(os.path.dirname(__file__), "..", "server")
sys.path.insert(0, _SERVER)

import decision_event as de  # noqa: E402


def _cv(target_id: str, verdict: str = "done", **kw) -> dict:
    """完遂 decision の record を 1 件組み立てる (builder 経由 = decision_id 採番込み)。"""
    return de.build_decision_event(
        kind="completion-verdict", decision=verdict,
        who={"session_id": "sv1", "user_id": "u"},
        related={"target_id": target_id}, **kw)


# ---------------------------------------------------------------------------
# 1. 規則の境界
# ---------------------------------------------------------------------------

def test_key_is_target_kind_and_verdict():
    key = de.completion_dedup_key(
        {"kind": "completion-verdict", "decision": "done",
         "related": {"target_id": "ms-9"}})
    assert key == ("ms-9", "completion-verdict", "done")


def test_non_completion_kinds_are_never_deduped():
    # dm-send / review-adjudication / task-done は同じ内容が正当に反復しうる
    # (同じ PR を二度採否する、同じ相手に二度送る)。ここで止めてはならない。
    for kind in ("dm-send", "review-adjudication", "task-done", "trek-review",
                 "scope-approval", "halt", "resume", "gate-judgement"):
        assert de.completion_dedup_key(
            {"kind": kind, "decision": "x",
             "related": {"target_id": "ms-9"}}) is None, kind


def test_completion_without_a_target_is_not_deduped():
    # どの target の完遂かが判らない行は重複判定の基準を持てない → 落とさず残す。
    assert de.completion_dedup_key(
        {"kind": "completion-verdict", "decision": "done"}) is None
    assert de.completion_dedup_key(
        {"kind": "completion-verdict", "decision": "done",
         "related": {"target_id": "  "}}) is None


def test_target_id_read_matches_the_read_window_fallback():
    # 窓の target 絞りと同じ規則 (_row_target_id) で読む: top-level target_id も拾う。
    # ここがズレると「list --target で引ける行が重複判定では見えない」穴になる。
    assert de.completion_dedup_key(
        {"kind": "completion-verdict", "decision": "done",
         "target_id": "ms-9"}) == ("ms-9", "completion-verdict", "done")


def test_duplicate_found_only_for_the_same_verdict_and_target():
    rows = [{"kind": "completion-verdict", "decision": "done",
             "related": {"target_id": "ms-9"}, "decision_id": "dec-A"}]
    same = {"kind": "completion-verdict", "decision": "done",
            "related": {"target_id": "ms-9"}}
    assert de.find_duplicate_completion(rows, same)["decision_id"] == "dec-A"
    # verdict が変わった = 正当な状態変化 → 重複ではない
    assert de.find_duplicate_completion(
        rows, {**same, "decision": "observing"}) is None
    # 別 target → 重複ではない
    assert de.find_duplicate_completion(
        rows, {**same, "related": {"target_id": "ms-10"}}) is None
    # 空 rows / None rows でも落ちない
    assert de.find_duplicate_completion([], same) is None
    assert de.find_duplicate_completion(None, same) is None


def test_duplicate_returns_the_oldest_match():
    # first-write-wins: 残すべきは最も古い記録なので、一致が複数あれば先頭を返す。
    rows = [
        {"kind": "completion-verdict", "decision": "done",
         "related": {"target_id": "ms-9"}, "decision_id": "dec-OLD"},
        {"kind": "completion-verdict", "decision": "done",
         "related": {"target_id": "ms-9"}, "decision_id": "dec-NEW"},
    ]
    assert de.find_duplicate_completion(
        rows, {"kind": "completion-verdict", "decision": "done",
               "related": {"target_id": "ms-9"}})["decision_id"] == "dec-OLD"


# ---------------------------------------------------------------------------
# 2. store の挙動 (dynamodb in-memory backend で round-trip)
# ---------------------------------------------------------------------------

def _fresh_dynamo():
    import dynamodb_client as dyn
    dyn._DECISION_EVENTS_FALLBACK.clear()
    return dyn


def test_same_target_same_verdict_lands_once_and_returns_the_first_id():
    dyn = _fresh_dynamo()
    pid = "proj-dedup"
    first = _cv("ms-9", created_at="2026-09-01T01:00:00.000000Z")
    second = _cv("ms-9", created_at="2026-09-15T01:00:00.000000Z")
    assert first["decision_id"] != second["decision_id"]

    id1 = dyn.append_decision_event(pid, first)
    id2 = dyn.append_decision_event(pid, second)

    # 二度目は append されず、既存行の id が返る (= 冪等な reject)。
    assert id1 == first["decision_id"]
    assert id2 == id1
    assert id2 != second["decision_id"]
    rows = dyn.list_decision_events(pid, kind="completion-verdict")
    assert len(rows) == 1
    assert rows[0]["created_at"] == "2026-09-01T01:00:00.000000Z"


def test_a_changed_verdict_is_kept_as_a_second_row():
    # observing で完遂した target が後に done へ倒れるのは正当な状態変化。
    dyn = _fresh_dynamo()
    pid = "proj-stages"
    dyn.append_decision_event(pid, _cv("ms-9", "observing",
                                       created_at="2026-09-01T01:00:00.000000Z"))
    dyn.append_decision_event(pid, _cv("ms-9", "done",
                                       created_at="2026-09-02T01:00:00.000000Z"))
    rows = dyn.list_decision_events(pid, kind="completion-verdict")
    assert [r["decision"] for r in rows] == ["observing", "done"]


def test_other_targets_and_other_kinds_are_unaffected():
    dyn = _fresh_dynamo()
    pid = "proj-mixed"
    dyn.append_decision_event(pid, _cv("ms-9"))
    dyn.append_decision_event(pid, _cv("ms-10"))
    # 同一内容の dm-send を 2 件 — 完遂族でないので両方残る。
    for i in range(2):
        dyn.append_decision_event(pid, de.build_decision_event(
            kind="dm-send", decision="sent",
            who={"session_id": "sv1", "user_id": "u"},
            related={"target_id": "ms-9"},
            created_at=f"2026-09-0{i + 3}T01:00:00.000000Z"))
    rows = dyn.list_decision_events(pid)
    assert sum(1 for r in rows if r["kind"] == "completion-verdict") == 2
    assert sum(1 for r in rows if r["kind"] == "dm-send") == 2


def test_projects_are_isolated():
    dyn = _fresh_dynamo()
    dyn.append_decision_event("proj-X", _cv("ms-9"))
    # 別 project の同じ target は別の完遂 — 弾かれてはならない。
    dyn.append_decision_event("proj-Y", _cv("ms-9"))
    assert len(dyn.list_decision_events("proj-X")) == 1
    assert len(dyn.list_decision_events("proj-Y")) == 1


# ---------------------------------------------------------------------------
# 3. + 4. 3 backend すべてが規則を呼ぶ (構造抽出 + test-the-test)
# ---------------------------------------------------------------------------

_BACKENDS = ("dynamodb_client", "firestore_client", "mysql_client")
_RULE = "find_duplicate_completion"


def _called_names(tree: ast.AST, func_name: str) -> set[str]:
    """``func_name`` の本体から呼ばれている関数名を構造抽出する。

    substring 検索 (``"..." in source``) にしないのは、docstring / コメントに名前が
    出ているだけで素通りする false-pass を防ぐため (= guard が drift で赤くならない
    のが最悪の失敗)。実際の ``ast.Call`` のみを数える。
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            out = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    f = sub.func
                    if isinstance(f, ast.Name):
                        out.add(f.id)
                    elif isinstance(f, ast.Attribute):
                        out.add(f.attr)
            return out
    return set()


@pytest.mark.parametrize("mod_name", _BACKENDS)
def test_every_backend_enforces_the_completion_rule(mod_name):
    # 1 backend だけ落ちると、backend を切り替えた本番で初めて重複が復活する
    # (= 手元 green / 本番だけ壊れる silent な穴)。store 層が唯一の強制点なので、
    # route だけで checking するのでは A〜G の全 writer を覆えない。
    path = os.path.join(_SERVER, f"{mod_name}.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    called = _called_names(tree, "append_decision_event")
    assert _RULE in called, (
        f"{mod_name}.append_decision_event が {_RULE} を呼んでいません "
        f"(ms-166 e-6602 の冪等制約が 1 backend で外れている)。呼ばれている: "
        f"{sorted(called)}")


def test_the_backend_guard_actually_fails_on_drift():
    # test-the-test: 規則呼び出しを落とした backend を合成して、上の guard が
    # 本当に赤くなることを確かめる。docstring に名前が残っていても通さない。
    drifted = textwrap.dedent('''
        def append_decision_event(project_id, data):
            """find_duplicate_completion のことは忘れました。"""
            # find_duplicate_completion(rows, data)
            return "dec-1"
    ''')
    called = _called_names(ast.parse(drifted), "append_decision_event")
    assert _RULE not in called, (
        "guard が緩い: docstring / コメントの言及だけで通ってしまう")


@pytest.mark.parametrize("mod_name", _BACKENDS)
def test_backend_imports_the_rule_from_the_single_source(mod_name):
    # 規則は server/decision_event.py が唯一の定義元。backend 内に別実装を
    # 持たせると 3 つに分かれて drift する (窓 window_decision_events と同じ分界)。
    try:
        mod = importlib.import_module(mod_name)
    except Exception as exc:  # pragma: no cover - optional deps at import
        pytest.skip(f"{mod_name} import unavailable: {exc}")
    assert not hasattr(mod, _RULE), (
        f"{mod_name} が {_RULE} を自前で定義しています — 規則は "
        f"decision_event.py の単一真実源から import すること")
