"""判断記録 (decision) の書き込み失敗が無言で消えないことの検証 (ms-166 e-6757)。

## なぜ要るか

決定を書く経路は「監査の副作用」なので、失敗しても呼び出し元のフロー (採否・done・
承認) を壊してはならない。だからどの経路も best-effort で包まれている。問題は、その
包み方が ``except BaseException: pass`` だったこと — endpoint 障害も拒否も
``KeyboardInterrupt`` までまとめて無言で飲むので、**判断は適用されたのに監査 arm には
痕跡が無い**、という食い違いが誰にも気づかれずに起きる。

完遂 (completion-verdict) 側は e-5978 で塞がれ、失敗契約は単一真実源
``commands_shared.best_effort_decision_write`` に集約された。しかし **同型の病理が
3 経路に残っていた** (e-6757 が名指しした承認ゲートの処分記録だけではない):

  ``lib/cmd_target.py::_record_disposition_decision``   kind=disposition
  ``lib/cmd_task.py::_record_task_done_decision``       kind=task-done
  ``lib/cmd_pr.py::_record_review_decision``            kind=review-adjudication

1 経路だけ塞いで「構造で閉じた」と言わないために、このファイルは **全 writer を機械で
列挙する** 形で固定する (= 新しく足された writer も自動で検査対象に入る。名指しリストを
持つと、次に足した人がリストを更新し忘れて silent に戻る)。

## このファイルが pin するもの

1. ``lib/`` のどの関数も、``client.record_decision`` を承認済み受け口の外で呼ばない。
   例外は許可リスト 1 件 = ``beacon decision record`` 本体 (= 記録そのものが目的の
   前景コマンドなので、飲むのではなく stderr + 非ゼロ終了で落ちるのが正しい)。
2. その許可リストが「無言の経路を隠す抜け道」になっていないこと (= 許可した経路が
   実際にうるさいことを別に検査する)。
3. 1 の guard が実際に drift で赤くなること (= test-the-test)。旧形・docstring だけ
   言及・修正後 の 3 形で、赤/赤/緑 を確かめる。
4. 3 経路の実挙動 — 失敗は WARNING で可視化して飲む / ``SystemExit`` も同様 /
   ``KeyboardInterrupt`` は伝播する。
"""
from __future__ import annotations

import ast
import logging
import os
import sys
import textwrap

import pytest

_LIB = os.path.join(os.path.dirname(__file__), "..", "lib")
sys.path.insert(0, _LIB)

import cmd_pr            # noqa: E402
import cmd_target        # noqa: E402
import cmd_task          # noqa: E402
import commands_shared   # noqa: E402


# ---------------------------------------------------------------------------
# 判定器: 承認済み受け口の外で decision を書いている箇所を構造抽出する
# ---------------------------------------------------------------------------
#
# 承認済み受け口。いずれも最終的に best_effort_decision_write の失敗契約に委譲する
# (WARNING で可視化して飲む / KeyboardInterrupt は伝播)。
_APPROVED_SEAMS = frozenset({
    "best_effort_decision_write",        # 汎用 (単一真実源)
    "best_effort_completion_decision",   # 完遂用 thin wrapper → 汎用に委譲
    "record_completion_decision",        # 完遂の収束口 → 上に委譲 (e-6602)
})

# 「飲まずにうるさく落ちる」のが正しい前景コマンド。ここに足すときは、その経路が
# 実際にうるさいことを test_allowlisted_paths_are_loud_by_design で示すこと。
_LOUD_BY_DESIGN = frozenset({("cmd_decision.py", "cmd_decision_record")})


def _is_record_decision_call(node: ast.AST) -> bool:
    """``<client>.record_decision(...)`` の **実呼び出し** だけを拾う。

    substring 検索 (``"record_decision" in source``) にしないのは、docstring や
    コメントに名前が出ているだけで判定が揺れるのを防ぐため。実際、e-6757 の修正後は
    3 経路の docstring に ``except BaseException: pass`` の文字列が (病理の説明として)
    残っているので、文字列一致の guard は偽陽性と偽陰性を同時に起こす。
    収束口 ``record_completion_decision`` は ``ast.Name`` なので誤検出しない。
    """
    return (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "record_decision")


def _with_item_names(node: ast.With) -> set:
    out = set()
    for item in node.items:
        ctx = item.context_expr
        if isinstance(ctx, ast.Call):
            f = ctx.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


def unguarded_decision_writes(tree: ast.AST) -> list:
    """承認済み受け口の ``with`` の中に居ない decision 書き込みを ``[(関数名, 行), …]`` で返す。"""
    found = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        guarded = set()
        for w in ast.walk(fn):
            if not isinstance(w, (ast.With, ast.AsyncWith)):
                continue
            if _with_item_names(w) & _APPROVED_SEAMS:
                for sub in ast.walk(w):
                    if _is_record_decision_call(sub):
                        guarded.add(id(sub))
        for sub in ast.walk(fn):
            if _is_record_decision_call(sub) and id(sub) not in guarded:
                found.append((fn.name, sub.lineno))
    return found


def _lib_files() -> list:
    return sorted(f for f in os.listdir(_LIB) if f.endswith(".py"))


# ---------------------------------------------------------------------------
# 1. 全 writer が受け口を通っている (許可リストを除く)
# ---------------------------------------------------------------------------

def test_no_silent_decision_writes_in_lib():
    offenders = []
    for name in _lib_files():
        src = open(os.path.join(_LIB, name), encoding="utf-8").read()
        if "record_decision" not in src:
            continue
        for fn, line in unguarded_decision_writes(ast.parse(src)):
            if (name, fn) in _LOUD_BY_DESIGN:
                continue
            offenders.append(f"{name}:{line} in {fn}()")
    assert not offenders, (
        "承認済み受け口の外で decision を書いている箇所があります "
        f"(ms-166 e-6757 — 失敗が無言で消える): {offenders}。"
        f"`with best_effort_decision_write(...)` で包むか、飲まずにうるさく落ちる "
        f"前景コマンドなら _LOUD_BY_DESIGN に足して、うるさいことを別に示してください。")


def test_every_approved_seam_exists():
    # 受け口の名前を綴り間違えると guard は「全部守られている」と誤答する
    # (= 存在しない名前は どの with にも一致しないのではなく、_APPROVED_SEAMS 側が
    #    空振りして offenders を増やす方向に倒れるが、名前が消えた場合は検知したい)。
    for seam in _APPROVED_SEAMS:
        assert hasattr(commands_shared, seam), (
            f"受け口 {seam} が commands_shared に在りません — 改名したなら "
            f"_APPROVED_SEAMS も更新してください")


# ---------------------------------------------------------------------------
# 2. 許可リストが抜け道になっていない
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("filename,func", sorted(_LOUD_BY_DESIGN))
def test_allowlisted_paths_are_loud_by_design(filename, func):
    """許可した経路は「飲む」のではなく「うるさく落ちる」ことを示す。

    これが無いと _LOUD_BY_DESIGN が「guard を黙らせる手段」になり、無言の経路を
    1 行足すだけで隠せてしまう (= 緑の guard が偽の安全を売る最悪の形)。
    """
    tree = ast.parse(open(os.path.join(_LIB, filename), encoding="utf-8").read())
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func:
            target = node
            break
    assert target is not None, f"{filename} に {func} が在りません"
    called = set()
    stderr_prints = 0
    for sub in ast.walk(target):
        if isinstance(sub, ast.Call):
            f = sub.func
            name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
            called.add(name)
            if name == "print":
                for kw in sub.keywords:
                    if kw.arg == "file":
                        stderr_prints += 1
    assert "exit" in called, (
        f"{filename}:{func} は許可リストに在りますが sys.exit を呼んでいません "
        f"— 失敗を飲んでいるなら受け口を通してください")
    assert stderr_prints, (
        f"{filename}:{func} は許可リストに在りますが失敗を stderr に出していません")


# ---------------------------------------------------------------------------
# 3. test-the-test: guard が drift で赤くなる / 修正後は緑になる
# ---------------------------------------------------------------------------

_OLD_SHAPE = textwrap.dedent('''
    def _record_disposition_decision(target_id, task_id, verdict, reason, source):
        """best-effort, cloud-only."""
        try:
            client, config = _get_api_client()
            client.record_decision(config["project_id"], {"kind": "disposition"})
        except BaseException:
            pass
''')

_DOCSTRING_ONLY = textwrap.dedent('''
    def _record_disposition_decision(target_id, task_id, verdict, reason, source):
        """失敗契約は best_effort_decision_write が持つ、とだけ書いてある。

        with best_effort_decision_write("disposition"):  # これは例示であって実行されない
        """
        client, config = _get_api_client()
        client.record_decision(config["project_id"], {"kind": "disposition"})
''')

_FIXED_SHAPE = textwrap.dedent('''
    def _record_disposition_decision(target_id, task_id, verdict, reason, source):
        """失敗は WARNING で可視化して飲む。"""
        with best_effort_decision_write("disposition for task=t1"):
            client, config = _get_api_client()
            client.record_decision(config["project_id"], {"kind": "disposition"})
''')


def test_guard_fails_on_the_old_silent_shape():
    assert unguarded_decision_writes(ast.parse(_OLD_SHAPE)), (
        "guard が旧形 (except BaseException: pass) を見逃しました")


def test_guard_is_not_fooled_by_a_docstring_mention():
    # 緩い一致の guard が最も踏みやすい偽 green。受け口の名前が docstring に在るだけで
    # 「守られている」と読んだら、無ガードより悪い (緑は信頼されるため)。
    assert unguarded_decision_writes(ast.parse(_DOCSTRING_ONLY)), (
        "guard が緩い: docstring の言及だけで通ってしまう")


def test_guard_accepts_the_fixed_shape():
    # guard が何でも赤くする (= 無意味に厳しい) わけでもないことを示す。
    assert not unguarded_decision_writes(ast.parse(_FIXED_SHAPE)), (
        "guard が厳しすぎる: 受け口を通した正しい形まで赤くしています")


def test_guard_does_not_confuse_the_completion_choke_point():
    # record_completion_decision(client, ...) は ast.Name なので record_decision とは別物。
    src = textwrap.dedent('''
        def _record_completion_decision(target, verdict, reason):
            record_completion_decision(client, "p1", {}, target_id="ms-9", verdict=verdict)
    ''')
    assert not unguarded_decision_writes(ast.parse(src))


# ---------------------------------------------------------------------------
# 4. 3 経路の実挙動 (失敗は warn して飲む / SystemExit も / KeyboardInterrupt は伝播)
# ---------------------------------------------------------------------------

class _RaisingClient:
    def __init__(self, exc):
        self._exc = exc

    def record_decision(self, project_id, decision):
        raise self._exc


def _cloud(monkeypatch, exc):
    client = _RaisingClient(exc)
    monkeypatch.setattr(commands_shared, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(commands_shared, "_get_api_client",
                        lambda: (client, {"project_id": "p1"}))
    monkeypatch.setattr(commands_shared, "_session_kind_is_human", lambda: False)


def _exiting_client(monkeypatch):
    def _boom():
        raise SystemExit(1)
    monkeypatch.setattr(commands_shared, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(commands_shared, "_get_api_client", _boom)
    monkeypatch.setattr(commands_shared, "_session_kind_is_human", lambda: False)


# (呼び出し, log に出るべき語, 対象の識別子)
def _call_disposition():
    cmd_target._record_disposition_decision("ms-9", "e-1", "superseded", "理由", "judge")


def _call_task_done():
    cmd_task._record_task_done_decision("e-1", "全 AC 達成")


def _call_review():
    cmd_pr._record_review_decision("e-1", "approve", "レビュー合格", "autonomous-AI", [])


_WRITERS = (
    pytest.param(_call_disposition, "disposition", "ms-9", id="disposition"),
    pytest.param(_call_task_done, "task-done", "e-1", id="task-done"),
    pytest.param(_call_review, "review-adjudication", "e-1", id="review-adjudication"),
)


@pytest.mark.parametrize("call,kind,ident", _WRITERS)
def test_write_failure_is_warned_and_swallowed(monkeypatch, caplog, call, kind, ident):
    _cloud(monkeypatch, RuntimeError("502 endpoint down"))
    with caplog.at_level(logging.WARNING):
        call()   # 例外が伝播しないこと (= 呼び出し元のフローを壊さない)
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "decision write failed" in msgs, msgs
    assert kind in msgs, msgs
    assert ident in msgs, msgs
    assert "502 endpoint down" in msgs, msgs


@pytest.mark.parametrize("call,kind,ident", _WRITERS)
def test_keyboard_interrupt_propagates(monkeypatch, call, kind, ident):
    # 利用者の中断を監査の副作用が飲んではならない (旧 BaseException は飲んでいた)。
    _cloud(monkeypatch, KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        call()


@pytest.mark.parametrize("call,kind,ident", _WRITERS)
def test_system_exit_is_warned_and_swallowed(monkeypatch, caplog, call, kind, ident):
    # _get_api_client は設定エラーで sys.exit しうる。判断は既に適用済なので飲むが、
    # 無言にはしない。
    _exiting_client(monkeypatch)
    with caplog.at_level(logging.WARNING):
        call()
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "decision write skipped" in msgs, msgs
    assert kind in msgs, msgs


@pytest.mark.parametrize("call,kind,ident", _WRITERS)
def test_local_mode_writes_nothing_and_says_nothing(monkeypatch, caplog, call, kind, ident):
    # local mode は失敗ではない。WARNING を出すと「毎回壊れている」ように見える。
    monkeypatch.setattr(commands_shared, "_is_cloud_mode", lambda: False)
    monkeypatch.setattr(commands_shared, "_get_api_client",
                        lambda: pytest.fail("local mode で API client を取ってはならない"))
    with caplog.at_level(logging.WARNING):
        call()
    assert not [r for r in caplog.records if "decision write" in r.getMessage()]
