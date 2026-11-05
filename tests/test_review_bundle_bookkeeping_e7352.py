"""bundle が独立性の契約を自分で破っていた件 (ms-119 e-7352)。

何が起きていたか
----------------
``beacon review context`` が組む artifact (= 機械採取した差分) に ``.beacon/``
配下の差分がそのまま入っていた。``.beacon/project.json`` には ``beacon log`` の
behavior、セッションログの summary、タスクの motivation —— **まさに実装者
セッションの意図説明** が入っている。一方 bundle の独立性の契約は judge に
こう言っている:

    実装者セッションの会話文脈・コミットの意図説明・『本当はこうするつもり』を
    一切参照しないでください

つまり **bundle 自身が「読むな」と言ったものを運んでいた**。しかも
``project.json`` は差分の中で単体最大のファイルなので、judge が「大きいファイル
から読む」戦略を採ると最初に当たる。

実測 (ricordi-sales-ai、文字数は差分全体に対する ``.beacon/`` の比)

    2026-11-02  PR #5 の準備   26%
    2026-11-04  PR #5          26%   judge 3 体が独立に指摘
    2026-11-05  PR #6          70%   project.json 単体で 1,106,326 字

3 回とも judge は「読まずに飛ばした」と**自己申告**しただけで、構造では閉じて
いなかった。これは Beacon が他のあらゆる場所で退けている形 (= お願いで閉じる)
である。読んではいけないなら、送ってはいけない。

この file が留めるもの
----------------------
1. ``.beacon/`` が差分から落ちること (rename の片側一致でも落ちる)
2. **落としたことが gaps に出ること**。黙って削ると judge は「差分の全部を見た」と
   思ったまま判定する —— この穴を見つけた指摘そのものが「bundle が中身を偽る」
   話だったので、直し方が同じ偽りを作ってはならない
3. 落とす処理が **差分収集の 1 箇所** に居ること。収集は 4 経路 (review context /
   batch / 目的達成 / --diff-ref) あり、1 箇所だけ直して「全域に効く」と名乗ると
   e-6971 と同じ「片方だけ直る」形に戻る
4. 作業記録しか変えていない PR で、空の artifact を judge に渡さないこと
   (何も見ていない判定が ``review done`` で「実施した」になる)
"""

from __future__ import annotations

import ast
import inspect

import pytest

import commands
import review_spine as rs


def _section(path_a, path_b=None, body="@@ -1 +1 @@\n-old\n+new\n"):
    b = path_b or path_a
    return (f"diff --git a/{path_a} b/{b}\n"
            f"--- a/{path_a}\n"
            f"+++ b/{b}\n"
            f"{body}")


CODE = _section("engine/run.py")
LEDGER = _section(".beacon/project.json", body='@@ -1 +1 @@\n-{"summary": "前回の経緯"}\n+{"summary": "今回の経緯"}\n')
CHANGELOG = _section(".beacon/changelog.jsonl")


# --- 1. 落ちること ------------------------------------------------------------

def test_bookkeeping_sections_are_dropped():
    filtered, dropped = rs.strip_bookkeeping_from_diff(CODE + LEDGER + CHANGELOG)
    assert "engine/run.py" in filtered
    assert ".beacon/" not in filtered
    assert [p for p, _ in dropped] == [".beacon/project.json",
                                       ".beacon/changelog.jsonl"]


def test_reviewable_diff_is_untouched():
    filtered, dropped = rs.strip_bookkeeping_from_diff(CODE)
    assert filtered == CODE and dropped == []


def test_empty_and_headerless_input_survive():
    assert rs.strip_bookkeeping_from_diff("") == ("", [])
    assert rs.strip_bookkeeping_from_diff("no diff here\n") == ("no diff here\n", [])


def test_rename_drops_on_either_side():
    """作業記録に入る / から出る rename も落ちる。どちら向きでも中身は作業記録。"""
    into = _section("notes.md", ".beacon/notes.md", body="@@ -0,0 +1 @@\n+x\n")
    out_of = _section(".beacon/notes.md", "notes.md", body="@@ -1 +0,0 @@\n-x\n")
    for d in (into, out_of):
        filtered, dropped = rs.strip_bookkeeping_from_diff(CODE + d)
        assert len(dropped) == 1, d
        assert ".beacon/" not in filtered


def test_path_with_spaces_is_classified_by_file_headers():
    """``diff --git`` 行は空白混じりのパスで曖昧になるので file header を先に見る。"""
    d = _section(".beacon/my notes.json")
    filtered, dropped = rs.strip_bookkeeping_from_diff(d)
    assert [p for p, _ in dropped] == [".beacon/my notes.json"]
    assert filtered.strip() == ""


def test_preamble_before_first_section_is_kept():
    filtered, _ = rs.strip_bookkeeping_from_diff("Merge preamble\n" + LEDGER)
    assert filtered == "Merge preamble\n"


# --- 2. 黙って削らない --------------------------------------------------------

def test_drop_is_disclosed_with_what_and_how_much():
    _, dropped = rs.strip_bookkeeping_from_diff(CODE + LEDGER)
    gap = rs.bookkeeping_drop_gap(dropped)
    assert ".beacon/project.json" in gap
    assert "字" in gap                      # どれだけ外したかが数で載る
    assert "判定に入っていません" in gap      # 何を見ていないかを前提として伝える


def test_no_disclosure_when_nothing_dropped():
    assert rs.bookkeeping_drop_gap([]) == ""


def test_many_drops_are_summarised_not_truncated_silently():
    dropped = [(f".beacon/f{i}.json", 10) for i in range(8)]
    gap = rs.bookkeeping_drop_gap(dropped)
    assert "8 ファイル" in gap and "ほか 3 ファイル" in gap


# --- 3. 収集の 1 箇所に居ること -----------------------------------------------

def test_filter_lives_in_the_single_collection_chokepoint():
    """全 4 経路が通る ``_collect_change_diff`` の中で落とす。

    呼び出し側で個別に落とす形 (= 1 箇所だけ直る形) に戻っていないことを確かめる。
    """
    src = inspect.getsource(commands._collect_change_diff)
    assert "strip_bookkeeping_from_diff" in src
    assert "bookkeeping_drop_gap" in src

    whole = inspect.getsource(commands)
    assert whole.count("strip_bookkeeping_from_diff") == 1, (
        "差分から作業記録を落とすのは 1 箇所だけ。呼び出し側に増やすと、"
        "増えた収集経路が素通りする")


def test_every_change_diff_caller_goes_through_the_chokepoint():
    """差分を直接 subprocess で採る経路が新たに生えていないこと。"""
    tree = ast.parse(inspect.getsource(commands))
    offenders = []
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        if fn.name == "_collect_change_diff":
            continue
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call) and
                    isinstance(node.func, ast.Attribute) and
                    node.func.attr == "run"):
                continue
            argv = node.args[0] if node.args else None
            flat = ast.dump(argv) if argv is not None else ""
            if "'diff'" in flat and ("'git'" in flat or "'gh'" in flat):
                offenders.append(fn.name)
    assert not offenders, (
        f"差分を自前で採っている関数: {offenders}。収集は "
        f"_collect_change_diff を通す (そこで作業記録を外すため)")


# --- 4. 作業記録だけの PR で空の artifact を渡さない --------------------------

def _fake_run(stdout):
    class _P:
        returncode = 0
        stderr = ""
    p = _P()
    p.stdout = stdout
    return lambda *a, **k: p


def test_bookkeeping_only_pr_stops_instead_of_emitting_an_empty_artifact(monkeypatch):
    monkeypatch.setattr(commands.subprocess, "run", _fake_run(LEDGER))
    gaps = []
    with pytest.raises(SystemExit):
        commands._collect_change_diff(["gh", "pr", "diff", "6"], "o/r#6", gaps,
                                      empty_is_error=True)


def test_bookkeeping_only_diffref_keeps_the_bundle_but_says_so(monkeypatch):
    """``--diff-ref`` は空も正当な答えなので止めない。ただし黙らない。"""
    monkeypatch.setattr(commands.subprocess, "run", _fake_run(LEDGER))
    gaps = []
    out = commands._collect_change_diff(["git", "diff", "-U25", "a...b"],
                                        "a...b", gaps)
    assert out.strip() == ""
    assert any("作業記録" in g for g in gaps)
    assert any("レビュー対象の変更が" in g for g in gaps)


def test_mixed_diff_passes_through_with_disclosure(monkeypatch):
    monkeypatch.setattr(commands.subprocess, "run", _fake_run(CODE + LEDGER))
    gaps = []
    out = commands._collect_change_diff(["gh", "pr", "diff", "6"], "o/r#6", gaps,
                                        empty_is_error=True)
    assert "engine/run.py" in out and ".beacon/" not in out
    assert len(gaps) == 1 and ".beacon/project.json" in gaps[0]
