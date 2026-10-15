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


def test_the_ledger_declares_every_path_of_the_flagship_kinds():
    """台帳が旗艦 2 種別の **全ての経路** を宣言していること。"""
    import capability_ledger as cl
    for kind in ("task-done", "review-adjudication"):
        assert cl.decision_capture_requires_all(kind), kind
    for producer in ("_stage_task_done_decision",
                     "_stage_operation_task_done_decision",
                     "decision_event_from_task_done"):
        assert producer in cl.DECISION_CAPTURE_PRODUCERS["task-done"], producer
    assert ("_record_review_adjudication_decision"
            in cl.DECISION_CAPTURE_PRODUCERS["review-adjudication"])


def test_the_any_of_escape_cannot_be_used_to_re_weaken_coverage():
    """``any`` に戻す逃げ道が、**辞書に 1 行足すだけでは使えない** こと。

    独立保守性レビュー medium: 被覆を ``all`` に厳格化したが、
    ``DECISION_CAPTURE_ALTERNATE_PRODUCERS`` に kind 名と自由文を書けばその種別だけ
    ``any`` に戻せる。そして初版のこの試験は **2 つの kind 名をハードコード** して
    いただけで、辞書の全エントリを走査していなかった —— つまり CI を緑にしたいだけの
    次の人が 1 行足して「代替経路」と書けば通る。「黙って any に戻せる」として問題視
    したのと同型の抜け道を、名前を変えて 1 段上に温存していた。

    そこで **全エントリ** に対して、「代替」の意味を機械で確かめる: 代替と宣言できる
    のは、宣言された producer が **実際に同一の実装に還元される** 場合だけ
    (halt / resume のように 1 つの builder が kind を切り替える形)。異なる実装を
    「代替」と称して緩めることはできない。
    """
    import capability_ledger as cl
    for kind, why in cl.DECISION_CAPTURE_ALTERNATE_PRODUCERS.items():
        assert kind in cl.DECISION_CAPTURE_PRODUCERS, (
            f"{kind} は producer 台帳に無い (綴り間違い / 消えた種別)")
        assert why.strip(), f"{kind} の代替理由が空 (黙って緩い側に倒れない)"
        producers = set(cl.DECISION_CAPTURE_PRODUCERS[kind])
        assert len(producers) == 1, (
            f"{kind} を『代替』として any 判定に緩めているが、宣言された producer が "
            f"{sorted(producers)} と複数ある。代替と言えるのは 1 つの実装に還元される "
            f"場合だけ —— 別々の経路なら全数配線 (all) を満たすべきで、ここに逃がすのは "
            f"被覆の再緩和にあたる。")


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


def test_every_call_site_declares_whether_it_captures():
    """``task_done`` / ``operation_task_done`` の **本番の全呼び出し口** が
    ``capture_decision=`` を明示していること (ms-166 e-6997)。

    これが要るのは、積んだのに flush されない経路が **無言で溜まる** 形を独立
    レビュー 2 体が実測したから: server の done route は ``core.task_done`` を呼ぶが
    保存が ``commands_shared.save_project`` を通らないので flush が走らない。
    「この保存経路は flush するか」を呼び出し側が述べる形にしたので、**述べている
    ことを構文木で数える** (文字列一致だと docstring の言及に反応する)。
    """
    import ast
    import io as _io

    targets = {"task_done", "operation_task_done"}
    offenders = []
    seen = 0
    for rel in ("lib", "server"):
        base = os.path.join(ROOT, rel)
        for name in sorted(os.listdir(base)):
            if not name.endswith(".py"):
                continue
            path = os.path.join(base, name)
            tree = ast.parse(_io.open(path, encoding="utf-8").read())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                nm = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                if nm not in targets:
                    continue
                # 定義そのもの / 同名の別関数 (operation_task_done の定義側) は除く
                if isinstance(fn, ast.Name) and nm in targets and rel == "lib" \
                        and name == "core.py":
                    continue
                seen += 1
                if "capture_decision" not in {k.arg for k in node.keywords}:
                    offenders.append(f"{rel}/{name}:{node.lineno} ({nm})")
    assert seen >= 3, (
        f"呼び出し口を {seen} 件しか見ていない — この試験が実表面を捉えていない "
        f"(CLI 2 + server 1 が最低限)")
    assert offenders == [], (
        "capture_decision を明示していない呼び出し口がある (積んだのに flush されない "
        "経路を黙って作れる):\n  " + "\n  ".join(offenders))


def test_staging_without_a_flush_cannot_grow_without_bound(capsys):
    """flush されない経路から積み続けても、**無言で伸びない** こと (ms-166 e-6997)。

    独立 AX / 保守性レビューが **独立に同じ high** を挙げた形: server の done route は
    ``core.task_done`` を呼ぶが保存が ``commands_shared.save_project`` を通らないので
    flush が走らず、積んだ payload がプロセスグローバルに溜まり続けていた (例外もログも
    出ず、症状はメモリだけが伸びること)。呼び出し側に明示させる旗を足したうえで、
    **万一また積む経路が生まれても上限で止まって名乗る** トリップワイヤーを置いた。
    """
    import decision_outbox

    limit = decision_outbox._STAGE_LIMIT
    data = _project()
    for i in range(limit + 20):
        decision_outbox.stage({"kind": "task-done", "decision": "done",
                               "related": {"task_id": f"e-{i}"}}, verify=None)

    assert len(decision_outbox.staged()) <= limit, (
        f"待ち行列が上限 {limit} を超えて伸びている")
    err = capsys.readouterr().err
    assert "flush していません" in err, f"上限超過を名乗っていない: {err!r}"
    assert err.count("flush していません") == 1, "1 プロセス 1 回だけ述べること"


def test_the_server_path_does_not_stage(monkeypatch):
    """``capture_decision=False`` の呼び出しは積まないこと (= server の経路)。"""
    import decision_outbox
    data = _project()
    core.task_done(data, "e-1", reason="server route", capture_decision=False)
    assert decision_outbox.staged() == (), (
        "flush されない経路なのに積んでいる")


def test_operation_task_done_also_stages_a_decision():
    """operation 配下のタスク完了も記録すること (ms-166 e-6997, AX medium)。

    台帳に producer を書くだけでは **呼ばれている保証にならない**: 反転ハーネスが
    「staging を外しても台帳の試験は緑」と報告した (= 私のテストの穴)。挙動で測る。
    """
    import decision_outbox
    data = {"name": "p", "summary": "", "operations": [
        {"id": "op-1", "title": "o", "status": "open", "entries": [
            {"id": "e-9", "type": "operation_task", "status": "todo",
             "description": "やること"}]}]}

    core.operation_task_done(data, "e-9", reason="片付いた",
                             capture_decision=True)

    staged = decision_outbox.staged()
    assert len(staged) == 1, f"operation task の完了が記録されていない: {staged}"
    payload, verify = staged[0]
    assert payload["kind"] == "task-done"
    assert payload["related"]["task_id"] == "e-9"
    assert "operation=op-1" in payload["context"], payload["context"]
    assert verify is not None and verify(data) is True
    # 保存に至らなかった世界では偽 (= 閉じていない完了を記録しない)。
    assert verify({"name": "p", "summary": "", "operations": [
        {"id": "op-1", "entries": [{"id": "e-9", "status": "todo"}]}]}) is False


def test_operation_task_done_respects_the_capture_flag():
    """``capture_decision=False`` なら積まないこと (= flush されない経路用)。"""
    import decision_outbox
    data = {"name": "p", "summary": "", "operations": [
        {"id": "op-1", "title": "o", "status": "open", "entries": [
            {"id": "e-9", "type": "operation_task", "status": "todo",
             "description": "x"}]}]}
    core.operation_task_done(data, "e-9", reason="x", capture_decision=False)
    assert decision_outbox.staged() == ()


def test_only_flushing_modules_may_capture():
    """**flush する経路だけが積む** ことを呼び出し口ごとに測る (ms-166 e-6997)。

    反転ハーネスが「server の呼び出し口を ``capture_decision=True`` に変えても緑」と
    報告した: 旧版の census は旗が **在るか** だけを見て、**値** を見ていなかった。
    積んだのに flush されない経路が無言で溜まる、という独立レビュー 2 体の high は
    まさにその値の話なので、値で測る。

    判定規則 (機械で導ける形): その file が ``save_project`` を呼ぶ (= CLI の保存 seam
    を通る) なら ``True`` を渡してよい。呼ばないなら ``False`` でなければならない。
    ``server/`` は ``commands_shared`` を import しないので構造的に後者。
    """
    import ast
    import io as _io

    targets = {"task_done", "operation_task_done"}
    problems = []
    checked = 0
    for rel in ("lib", "server"):
        base = os.path.join(ROOT, rel)
        for name in sorted(os.listdir(base)):
            if not name.endswith(".py") or (rel == "lib" and name == "core.py"):
                continue
            src = _io.open(os.path.join(base, name), encoding="utf-8").read()
            tree = ast.parse(src)
            flushes = any(
                isinstance(n, ast.Call)
                and (n.func.attr if isinstance(n.func, ast.Attribute)
                     else getattr(n.func, "id", "")) == "save_project"
                for n in ast.walk(tree))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                nm = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                if nm not in targets:
                    continue
                kw = {k.arg: k.value for k in node.keywords}
                val = kw.get("capture_decision")
                literal = getattr(val, "value", None) if isinstance(val, ast.Constant) else None
                checked += 1
                where = f"{rel}/{name}:{node.lineno} ({nm})"
                if literal is None:
                    problems.append(f"{where}: capture_decision が真偽値リテラルでない")
                elif literal and not flushes:
                    problems.append(
                        f"{where}: capture_decision=True だが この file は save_project を "
                        f"呼ばない (= flush されないので積んだ分は書かれないまま溜まる)")
                elif (not literal) and flushes:
                    problems.append(
                        f"{where}: capture_decision=False だが この file は save_project を "
                        f"呼ぶ (= 記録できる経路なのに記録していない)")
    assert checked >= 3, f"呼び出し口を {checked} 件しか見ていない (CLI 2 + server 1 が最低限)"
    assert problems == [], "積む / 積まないの宣言が保存経路と食い違っている:\n  " + "\n  ".join(problems)
