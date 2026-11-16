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
3. **差分を採る経路が全部、落とす処理を通ること**。1 箇所だけ直して「全域に効く」と
   名乗ると e-6971 と同じ「片方だけ直る」形に戻る。
   変更スコープの 4 経路 (review context / batch / 目的達成 / --diff-ref) は
   ``_collect_change_diff`` という 1 つの口を共有するが、**それが全経路ではない**:
   ``_collect_whole_target_artifact`` は target の台帳から 1 commit ずつ
   ``git show`` で採るので、この口を通らない (通せない — あの口は rc≠0 で
   ``sys.exit`` するが、whole-target は未 fetch の commit を許容して数え続けるのが
   仕様)。だから不変条件は「呼び出しが 1 箇所」ではなく「**採る関数は全部落とす**」
   側に置く。以前ここは前者 (文字列の出現が 1 回) を測っていたが、それは代理の
   指標で、`git show` で採る 6 番目の経路を視界に入れないまま「1 箇所に居る」と
   緑を出していた
4. 作業記録しか変えていない PR で、空の artifact を judge に渡さないこと
   (何も見ていない判定が ``review done`` で「実施した」になる)
"""

from __future__ import annotations

import ast
import glob
import os
import inspect

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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

def test_both_collectors_go_through_the_chokepoint():
    """差分を採る 2 つの関数が、どちらも唯一の口を通る。

    変更スコープの 4 経路は ``_collect_change_diff`` を共有し、target 全体は
    ``_collect_whole_target_artifact`` が 1 commit ずつ採る。後者は前者を通せない
    (rc≠0 の扱いが逆で、未 fetch の commit を許容するのが仕様) が、**どちらも
    ``_run_diff_command`` は通る** —— 落とす処理はそこに 1 つだけ在る。
    """
    for fn in (commands._collect_change_diff,
               commands._collect_whole_target_artifact):
        src = inspect.getsource(fn)
        assert _CHOKEPOINT in src, fn.__name__
        assert "strip_bookkeeping_from_diff" not in src, (
            f"{fn.__name__} が自前で落としている。落とすのは {_CHOKEPOINT} の 1 箇所だけ")
    # 唯一の口は、落として・何を落としたかを返す
    choke = inspect.getsource(commands._run_diff_command)
    assert "strip_bookkeeping_from_diff" in choke
    # 開示は呼び出し側が行う (口ごとに形が違うため)
    assert "bookkeeping_drop_gap" in inspect.getsource(commands._collect_change_diff)
    assert "bookkeeping_drop_gap" in inspect.getsource(
        commands._collect_whole_target_artifact)


# ---------------------------------------------------------------------------
# 差分を得る経路を 1 つに閉じる (ms-119 e-7352 / 独立 AX レビュー 2026-11-16)
# ---------------------------------------------------------------------------
#
# 以前ここは「差分を採る関数は strip を呼んでいるか」を静的に測っていた。独立 judge が
# 実際に 4 つの形を書いて、**全部すり抜けて緑のまま通る**ことを実測した:
#
#   1. argv を変数に束縛してから渡す      → ast.List の要素を見る判定に掛からない
#   2. `git log -p` で採る                → 下位コマンドの集合が {diff, show} 固定だった
#   3. 別モジュールに置く                  → commands.py しか歩いていなかった
#   4. strip の名前を docstring に書くだけ → `"名前" in ast.dump(fn)` の部分一致で通った
#
# 4 は「緩い部分一致は偽の通過を作る」という、この repo が何度も踏んでいる型そのもの。
# **緑は信頼されるので、偽の安全は無ガードより悪い**。
#
# そこで「呼んでいるかを測る」のをやめ、**落とさずに差分を得る経路を無くした**
# (lib/commands.py の `_run_diff_command` が唯一の口で、そこで必ず落ちる)。
# 検査はそれに合わせて 2 本になる:
#
#   A. strip の呼び出しは repo 全体でちょうど 1 つで、それは唯一の口の中にある
#   B. commands.py の中で git / gh を走らせる箇所は、唯一の口の中か、
#      **差分を産まないと証明できる**もののどちらかである (既定は拒否)
#
# B の既定を拒否にしたのが 1〜3 への答え: 知らない下位コマンドも、argv が literal で
# なくて中身を確かめられないものも、「通してよい」ではなく「口を通せ」に倒れる。
#
# **閉じていない範囲 (正直に書く)**: B は lib/commands.py だけを歩く。レビューの収集は
# 現在ここにしか無いが、別モジュールに新しい収集経路を置かれた場合は A しか効かない
# (A は「strip が増えたか」は見るが「落とさない収集が増えたか」は見ない)。e-7515 で追う (別ファイルの収集経路)。

_CHOKEPOINT = "_run_diff_command"

# git の下位コマンドのうち、差分を産まないと言い切れるもの。**ここに無いものは拒否**。
_PROVEN_NON_DIFF_GIT = {
    "rev-parse", "config", "tag", "branch", "fetch", "checkout", "merge-base",
    "worktree", "describe", "ls-files", "cat-file", "status", "remote",
    "symbolic-ref", "rev-list", "show-ref", "pull", "push", "merge", "init",
    "clone", "add", "commit", "stash", "reflog",
}
# 差分を要求する旗。log / show に付くと差分になる。
_PATCH_FLAGS = {"-p", "-u", "--patch", "-U", "--unified"}
# show に付くと差分でなくなる旗。
_NAMES_ONLY_FLAGS = {"--name-only", "--name-status"}
# gh の下位コマンドで差分を産むもの。
_DIFF_GH = {"diff"}

# 差分を採れてしまう汎用ランナー (argv を外から受ける) の名前。呼び出し側を検査する。
_GENERIC_RUNNERS = {"_git_ok"}


def _argv_tokens(node):
    """``(tokens, opaque)`` — argv の要素列と、「中身が全く見えないか」。

    リストで渡されていれば要素ごとに定数かを見る (定数でない要素は ``None``)。
    **リストですらない** (変数そのものを渡している) 場合は ``opaque=True``。
    opaque は中身を一切確かめられないので拒否側に倒す —— これを通すと
    ``argv = ["git", "diff", ref]; subprocess.run(argv)`` が素通りする
    (独立 judge が実際に書いてすり抜けさせた形)。
    """
    if not isinstance(node, ast.List):
        return [], True
    toks = []
    for e in node.elts:
        toks.append(e.value if isinstance(e, ast.Constant)
                    and isinstance(e.value, str) else None)
    return toks, False


def _git_subcommand(tokens):
    """グローバル旗 (-C <path> 等) を読み飛ばして下位コマンドを返す。"""
    i = 1
    while i < len(tokens):
        t = tokens[i]
        if t is None:
            return None
        if t == "-C" or t == "-c":
            i += 2
            continue
        if t.startswith("--git-dir") or t.startswith("--work-tree"):
            i += 1
            continue
        if t.startswith("-"):
            i += 1
            continue
        return t
    return None


# 差分 (patch) を出しうる下位コマンド。これ以外は引数が何であれ patch を吐かない。
_DIFF_CAPABLE_GIT = {"diff", "show", "log", "format-patch", "range-diff", "whatchanged"}


def _is_provably_non_diff(tokens, opaque):
    """差分を産まないと **証明できる** か。証明できないものは False (= 拒否)。

    引数が変数なだけで拒否すると偽の赤になる (``git cat-file -e <sha>`` や
    ``git -C <path> pull`` は中身が何であれ patch を吐かない)。見るべきは
    「下位コマンドが差分を出しうるか」で、出しうる場合に限って
    「差分でないこと」を定数で示させる。
    """
    if opaque:
        return False          # 中身が全く見えない = 通さない
    if not tokens:
        return True
    if tokens[0] is None:
        # リストだがプログラム名が変数。git/gh と静的に言えないので対象外にする
        # (beacon 自身を叩く probe 等。ここを拒否にすると偽の赤が増える)。
        return True
    if tokens[0] not in ("git", "gh"):
        return True           # 対象外
    sub = _git_subcommand(tokens)
    if sub is None:
        return False          # 下位コマンドを確かめられない
    if tokens[0] == "gh":
        return sub not in _DIFF_GH and "diff" not in tokens
    if sub not in _DIFF_CAPABLE_GIT:
        return True           # 引数が何であれ patch にならない
    # ここから下は patch を出しうる。差分でないことを **定数で** 示せたときだけ通す。
    if any(f in tokens for f in _NAMES_ONLY_FLAGS):
        return True
    if sub == "format-patch":
        return False
    if any(t is None for t in tokens):
        return False          # 変数が混ざる = その中身が -p かもしれない
    if sub == "log":
        return not any(t in _PATCH_FLAGS or t.startswith("-U") for t in tokens)
    return False              # diff / show / range-diff / whatchanged は既定で拒否


def _enclosing_function(parents, node):
    f = node
    while f in parents:
        f = parents[f]
        if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return f.name
    return "<module>"


def _list_assignments(fn):
    """関数内の ``name = [...]`` を ``{name: [tokens, ...]}`` に集める。

    ``argv = ["git", "diff", ref]`` のように一旦変数へ入れてから渡す形を
    **禁止せず解決する**。禁止すると正当な書き方 (``gh pr view`` を組み立てて
    渡す既存コード) が偽の赤になり、解決しないと独立 judge が実測した
    「変数経由ですり抜ける」穴が開いたままになる。
    同じ名前に複数回代入していれば **全部** を候補に持ち、1 つでも差分を
    産みうるなら offender 扱いにする (分岐で差し替える形を見逃さない)。
    """
    out = {}
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.List):
            continue
        for tgt in node.targets:
            if isinstance(tgt, ast.Name):
                out.setdefault(tgt.id, []).append(_argv_tokens(node.value)[0])
    return out


def _invocation_sites(tree):
    """``[(関数名, tokens, opaque)]`` — git/gh を走らせうる箇所。

    ``subprocess.run(...)`` と、argv を外から受ける汎用ランナー
    (``_git_ok`` 等) の呼び出しの両方を見る。後者を見ないと
    ``_git_ok(["git", "diff", ...])`` で採る形が素通りする。
    """
    parents = {}
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n):
            parents[c] = n
    fn_nodes = {}
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn_nodes[n.name] = n
    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        is_run = isinstance(node.func, ast.Attribute) and node.func.attr == "run"
        is_runner = isinstance(node.func, ast.Name) and node.func.id in _GENERIC_RUNNERS
        if not (is_run or is_runner):
            continue
        fname = _enclosing_function(parents, node)
        if is_run and fname in _GENERIC_RUNNERS:
            continue          # 汎用ランナー本体は素通し (呼び出し側で見る)
        arg = node.args[0]
        if isinstance(arg, ast.Name) and fname in fn_nodes:
            resolved = _list_assignments(fn_nodes[fname]).get(arg.id)
            if resolved:
                for toks in resolved:
                    sites.append((fname, toks, False))
                continue
        toks, opaque = _argv_tokens(arg)
        sites.append((fname, toks, opaque))
    return sites


def test_strip_is_called_in_exactly_one_place_and_it_is_the_chokepoint():
    """検査 A: 落とす処理の呼び出しは repo 全体で 1 つ、唯一の口の中。"""
    hits = []
    for path in sorted(glob.glob(os.path.join(REPO_ROOT, "lib", "*.py"))):
        tree = ast.parse(open(path, encoding="utf-8").read())
        parents = {}
        for n in ast.walk(tree):
            for c in ast.iter_child_nodes(n):
                parents[c] = n
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and
                    isinstance(node.func, ast.Attribute) and
                    node.func.attr == "strip_bookkeeping_from_diff"):
                hits.append((os.path.basename(path),
                             _enclosing_function(parents, node)))
    assert hits, "落とす処理の呼び出しが 1 つも無い = 誰も落としていない"
    assert len(hits) == 1, f"落とす箇所が増えている: {hits}。唯一の口に寄せる"
    assert hits[0][1] == _CHOKEPOINT, f"落としているのが唯一の口の外: {hits[0]}"


def test_no_diff_can_be_obtained_outside_the_chokepoint():
    """検査 B: 差分を産みうる git/gh の起動は、唯一の口の中にしか無い。

    既定は拒否。知らない下位コマンドも、argv を確かめられない呼び出しも offender。
    """
    tree = ast.parse(inspect.getsource(commands))
    sites = _invocation_sites(tree)
    assert sites, "git/gh の起動が 1 つも見つからない = 述語が壊れている"
    offenders = [
        (fn, " ".join(str(t) for t in toks[:5]) or "(argv を確かめられない)")
        for fn, toks, opaque in sites
        if fn != _CHOKEPOINT and not _is_provably_non_diff(toks, opaque)
    ]
    assert not offenders, (
        f"唯一の口 ({_CHOKEPOINT}) の外で差分を採りうる箇所: {offenders}。"
        f"差分が要るなら {_CHOKEPOINT} を通す (そこで作業記録が落ちる)")


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
