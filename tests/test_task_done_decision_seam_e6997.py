"""タスクを閉じるどの経路からも判断記録が出ること (ms-166 e-6997)。

経緯
----
独立 judge (目的達成) が実測した:

    ``pr approve`` の自動完了は ``core.task_done`` を直に呼ぶので、**task-done の
    判断記録を 1 件も残さない**。本番の直近 300 件で task-done は 39 件あるが、
    PR 承認由来は 0 件。AI が単独で下した done 判断こそ最も監査が要るのに、
    そこだけが落ちていた。しかも種別単位の checker からは見えない。

記録を書いていたのは CLI の handler (``cmd_task._record_task_done_decision``) だけ
だった = **1 つの入口にだけ関門を置き、同じ状態 (タスクが done) に到達する兄弟の
経路には置かない** という、この MS が何度も直している型。

直した形: 記録を **全ての経路が通る唯一の場所** (データ層の ``core.task_done``) に
移し、``decision_outbox`` に積んで保存後に flush する。保存後の data でそのタスクが
本当に done かを述語で確認してから書くので、「判断したことになっているがタスクは
閉じていない」記録が構造的に作れない。
"""
import copy
import os
import sys

import pytest

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
LIB = os.path.join(ROOT, "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import core             # noqa: E402
import decision_outbox  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_outbox():
    decision_outbox.discard()
    yield
    decision_outbox.discard()


def _project():
    return {"name": "p", "summary": "", "milestones": [
        {"id": "ms-9", "title": "t", "status": "in_progress", "progress": 0,
         "entries": [
             {"id": "e-1", "type": "task", "status": "todo",
              "description": "やること", "date": ""},
             {"id": "e-2", "type": "task", "status": "todo",
              "description": "別のやること", "date": ""},
         ]}]}


# --------------------------------------------------------------------------
# 空振り防止 — 溶接が生きていることを先に固定する
# --------------------------------------------------------------------------

def test_closing_a_task_stages_a_decision():
    data = _project()
    core.task_done(data, "e-1", reason="AC 全達成")

    staged = decision_outbox.staged()
    assert len(staged) == 1, f"判断記録が積まれていない: {staged}"
    payload, verify = staged[0]
    assert payload["kind"] == "task-done"
    assert payload["decision"] == "done"
    assert payload["rationale"] == "AC 全達成"
    assert payload["related"]["task_id"] == "e-1"
    assert verify is not None, "保存後の検証述語が無い"


# --------------------------------------------------------------------------
# 本題 — どの入口から閉じても記録が出る
# --------------------------------------------------------------------------

def test_every_path_that_closes_a_task_records_the_decision():
    """CLI の handler を経ない呼び出し (= PR 承認の自動完了が通る形) でも出ること。

    ``core.task_done`` を直に呼ぶのが PR 承認の経路そのもの。以前はこの呼び方だと
    記録が 0 件だった。
    """
    data = _project()
    core.task_done(data, "e-1", reason="PR が解決した",
                   ac_waive="自動完了のため節は未判定",
                   decided_by="autonomous-AI",
                   evidence=["pr:800", "commit:abc1234"])

    payload, _ = decision_outbox.staged()[0]
    assert payload["decided_by"] == "autonomous-AI"
    assert payload["evidence"] == ["pr:800", "commit:abc1234"]
    assert payload["acceptance_waiver"].startswith("自動完了")
    assert "acceptance=waived" in payload["context"]


def test_the_verify_predicate_refuses_a_task_that_did_not_close():
    """保存後にそのタスクが done になっていなければ書かない。

    「判断したことになっているがタスクは閉じていない」記録を構造的に作れない形。
    """
    data = _project()
    core.task_done(data, "e-1", reason="x")
    _, verify = decision_outbox.staged()[0]

    assert verify(data) is True
    # 保存に至らなかった世界 (= 元の状態) では偽。
    assert verify(_project()) is False


def test_the_acceptance_verdicts_reach_the_record():
    """受入条件の節ごとの判定が記録に載ること (= なぜ done と言えるかの核心)。"""
    data = _project()
    # 形は terminal_gate.ac_verdicts_from_env と同じ文字列 list。
    core.task_done(data, "e-1", reason="x",
                   ac_verdicts=["1:met:コミット abc1234 で満たした",
                                "2:partial:残りは follow-up"])
    payload, _ = decision_outbox.staged()[0]
    assert "1:met" in payload["context"] and "2:partial" in payload["context"]


def test_closing_two_tasks_stages_two_records():
    """PR 承認が複数タスクを閉じる形でも 1 件ずつ残ること。"""
    data = _project()
    core.task_done(data, "e-1", reason="a")
    core.task_done(data, "e-2", reason="b")
    assert [p["related"]["task_id"] for p, _ in decision_outbox.staged()] == ["e-1", "e-2"]


def test_the_cli_handler_no_longer_writes_its_own_record():
    """CLI 側の重複 writer が消えていること (= 二重記録にならない)。"""
    import cmd_task
    assert not hasattr(cmd_task, "_record_task_done_decision"), (
        "CLI 側の writer が残っている — データ層の溶接と二重に書く")


# --------------------------------------------------------------------------
# PR 承認の自動完了が実際にこの seam を通ること
# --------------------------------------------------------------------------

def test_pr_approve_auto_done_records_a_decision(monkeypatch):
    """``cmd_pr`` の自動完了が core.task_done 経由で記録を積むこと。"""
    import cmd_pr
    data = _project()
    pr_entry = {"id": "e-pr", "type": "pr", "description": "fix",
                "meta": {"url": "https://github.com/o/r/pull/800"},
                "entries": [{"type": "commit", "meta": {"hash": "abc1234def"},
                             "description": "fix (e-1)"}]}

    core.task_done(data, "e-1", reason="PR #800 が解決",
                   decided_by="autonomous-AI",
                   evidence=cmd_pr._auto_done_evidence(pr_entry, {"task_id": "e-1"}))

    payload, _ = decision_outbox.staged()[0]
    assert payload["decided_by"] == "autonomous-AI"
    assert payload["evidence"] == ["pr:800", "commit:abc1234"]


def test_auto_done_evidence_carries_real_links():
    """根拠が **実 link** であること (存在しない鍵を読んで常に空、を防ぐ)。

    最初に書いた版は ``judgement.get("hashes")`` を読んでいた。judgement dict は
    task_id / confidence / description / ac_matched / reason しか持たないので、
    **常に空**になる形だった (= 緑のまま根拠を落とす)。
    """
    import cmd_pr
    got = cmd_pr._auto_done_evidence(
        {"meta": {"url": "https://github.com/o/r/pull/42"},
         "entries": [{"type": "commit", "meta": {"hash": "abc1234def"}},
                     {"type": "note", "meta": {"hash": "ignored"}},
                     {"type": "commit", "meta": {"hash": "99887766"}}]},
        {"task_id": "e-1"})
    assert got == ["pr:42", "commit:abc1234", "commit:9988776"], got

    # 材料が無ければ空 (= 裏付けの無い判断として正直に残す。自己参照で埋めない)。
    assert cmd_pr._auto_done_evidence({"meta": {}, "entries": []}, {}) == []


# --------------------------------------------------------------------------
# 被覆の検査が経路単位であること
# --------------------------------------------------------------------------

def _load_scope_checker():
    """``scripts/check-capability-scope.py`` を module として読む (ハイフン名)。"""
    import importlib.util
    path = os.path.join(ROOT, "scripts", "check-capability-scope.py")
    spec = importlib.util.spec_from_file_location("_scope_checker", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_ledger_declares_both_paths_of_the_flagship_kinds():
    """台帳が旗艦 2 種別の **両方の経路** を宣言していること。"""
    import capability_ledger as cl
    for kind, why in cl.DECISION_CAPTURE_ALTERNATE_PRODUCERS.items():
        assert kind in cl.DECISION_CAPTURE_PRODUCERS, kind
        assert why.strip(), f"{kind} の代替理由が空 (黙って緩い側に倒れない)"
    for kind in ("task-done", "review-adjudication"):
        assert cl.decision_capture_requires_all(kind), kind
    assert "_stage_task_done_decision" in cl.DECISION_CAPTURE_PRODUCERS["task-done"]
    assert ("_record_review_adjudication_decision"
            in cl.DECISION_CAPTURE_PRODUCERS["review-adjudication"])


def test_producer_coverage_is_path_level_not_one_of(monkeypatch):
    """**checker 自身** が「producer 全数が配線されているか」を見ていること。

    最初に書いた版は台帳の中身しか見ておらず、**checker の判定を any に戻しても
    緑のまま**だった (反転ハーネスが捕まえた = 自分で偽の安全を作っていた)。
    なので checker を実際に走らせ、片方だけ配線された世界で gap が出るかを測る。
    """
    import capability_ledger as cl
    mod = _load_scope_checker()

    kind = "review-adjudication"
    producers = sorted(cl.DECISION_CAPTURE_PRODUCERS[kind])
    assert len(producers) >= 2, f"この試験は複数 producer の kind で測る: {producers}"

    # 片方だけ配線された世界を作る (他の kind は全部配線済みとして渡す)。
    all_tokens = set()
    for ps in cl.DECISION_CAPTURE_PRODUCERS.values():
        all_tokens |= set(ps)
    partially_wired = all_tokens - {producers[0]}
    monkeypatch.setattr(mod, "_wired_tokens", lambda _trees: partially_wired)
    monkeypatch.setattr(mod, "_load_trees", lambda _paths: [])

    gaps = mod.find_decision_capture_gaps()

    hit = [g for g in gaps if g["kind"] == kind]
    assert hit, (
        f"producer {producers[0]} が未配線なのに gap が出ない — checker が "
        f"「どれか 1 つ在れば緑」(種別単位) に戻っている: {gaps}")
    assert hit[0]["unwired"] == [producers[0]], hit[0]

    # 逆側: 代替扱いの kind は 1 つ在れば緑のまま (緩めた宣言が効いている)。
    alt = next(iter(cl.DECISION_CAPTURE_ALTERNATE_PRODUCERS), "")
    if alt:
        alt_producers = sorted(cl.DECISION_CAPTURE_PRODUCERS[alt])
        if len(alt_producers) >= 2:
            monkeypatch.setattr(mod, "_wired_tokens",
                                lambda _t: all_tokens - {alt_producers[0]})
            assert not [g for g in mod.find_decision_capture_gaps()
                        if g["kind"] == alt]
