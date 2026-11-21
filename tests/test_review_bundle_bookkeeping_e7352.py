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

# 差分を要求する旗。log / show に付くと差分になる。
_PATCH_FLAGS = {"-p", "-u", "--patch", "-U", "--unified"}
# show に付くと差分でなくなる旗。
# 本文 (hunk) を出さない旗。これが付けば .beacon の中身は judge に渡りえない。
_NAMES_ONLY_FLAGS = {"--name-only", "--name-status", "--stat", "--numstat",
                     "--shortstat", "--dirstat", "--summary"}
# gh の下位コマンドで差分を産むもの。
# gh で差分を出す口は実質 `gh pr diff` だけ。argv 全体の生文字列一致にすると
# `gh issue list --label diff` のような無関係な呼び出しが赤くなる (独立 AX
# レビューが実測)。偽の赤はガードを緩める動機になるので、下位コマンドで絞る。
_DIFF_CAPABLE_GH = {("pr", "diff")}

# 差分を採れてしまう汎用ランナー (argv を外から受ける) の名前。呼び出し側を検査する。
_GENERIC_RUNNERS = {"_git_ok"}

# 外部コマンドを走らせる API。run だけ見ると check_output / Popen で素通りする
# (独立 judge が実測)。
_SUBPROCESS_APIS = {"run", "check_output", "check_call", "call", "Popen",
                    "popen", "getoutput", "getstatusoutput",
                    # os 側の口も見る。os.system は列挙に無く素通りしていた
                    # (独立保守性レビューが実測)。
                    "system", "posix_spawn", "posix_spawnp",
                    "spawnl", "spawnv", "spawnlp", "spawnvp"}

# 上の API が「本当に外部コマンドを走らせる口」であると言えるのは、呼び出し先が
# この 2 モジュールのどれかに由来するときだけ。
_RUNNER_MODULES = {"subprocess", "os"}


def _runner_bindings(tree):
    """``(module_aliases, bare_names, alias_attrs)`` — import 文と代入から「呼び出し先の素性」を引く表。

    **なぜ名前だけで判定してはいけないか** (独立 AX レビュー 2026-11-16 が
    両方向から実測):

    * 見落とし: ``from subprocess import run as _r`` と束縛してから ``_r([...])``
      と呼ぶ形は属性アクセスではないので、属性名だけを見る述語の視界に入らない。
      judge が実際にその形で収集関数を足し、検査が緑のまま通ることを確認した。
    * 偽の赤: 逆に ``pipeline.run(payload)`` のような、差分と無関係な
      オブジェクトの ``run()`` 呼び出しが offender になる (argv が List リテラル
      でないので opaque 扱いになり拒否側へ倒れる)。しかもメッセージは
      「唯一の口の外で差分を採りうる箇所」と原因を誤って名指しする。``run`` は
      Python でありふれた名前なので確実に踏まれ、踏んだ人は述語を緩める方向に
      直してしまう —— 偽の赤はガードを弱める動機になる。

    束縛元を import 文から解決すれば、この 2 つは 1 つの修正で同時に塞がる。

    ``module_aliases``: ``subprocess`` / ``os`` を指す名前 → 由来モジュール名。
    ``import subprocess as sp`` / ``import os as _os`` の別名も入る。
    ``bare_names``: ``from subprocess import run as _r`` のように裸の名前へ
    束縛された **実行 API だけ** → ``(モジュール, 元の名前)``。``PIPE`` のような
    実行しない名前は入れない。

    ``alias_attrs``: ``self.p = subprocess`` のように **属性へ** モジュールを
    入れた属性名。受け手を区別せず、``<何か>.p.run(...)`` を実行 API とみなす。

    **閉じていない範囲 (正直に書く)**: 関数の中の import / 代入も同じ 1 つの表に
    集めるので、別の関数で同じ名前を使うと解決されたことになる (拒否側に倒れるので
    安全側だが、精度としては粗い)。``from subprocess import *``、関数の引数や戻り値で
    モジュールを受け渡す形、``getattr(subprocess, "run")`` は解決しない。
    """
    module_aliases = {}
    bare_names = {}
    alias_attrs = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in _RUNNER_MODULES:
                    module_aliases[a.asname or a.name] = a.name
        elif isinstance(node, ast.ImportFrom):
            if node.module not in _RUNNER_MODULES or node.level:
                continue
            for a in node.names:
                if a.name in _SUBPROCESS_APIS:
                    bare_names[a.asname or a.name] = (node.module, a.name)
    # **代入で別名を作る形も追う** (PR #850 独立 AX、実測): import だけを見ると
    # ``sp = subprocess`` / ``self.p = subprocess`` / ``f = subprocess.run`` の後の
    # 呼び出しが視界から外れ、差分を採る経路が緑のまま足せた (属性名だけで
    # 判定していた変更前は、前の 2 つは拒否側に倒れていた)。代入の連鎖
    # (``a = subprocess; b = a``) も追えるよう、増えなくなるまで回す。
    # 属性への代入は受け手を区別しない (``self.p`` に入れて ``s.p.run`` と呼ぶ形を
    # 拾うため) —— 拒否側に倒れる粗さで、安全側。
    assigns = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)]
    changed = True
    while changed:
        changed = False
        for node in assigns:
            val = node.value
            for tgt in node.targets:
                if isinstance(val, ast.Name) and val.id in module_aliases:
                    if isinstance(tgt, ast.Name) and tgt.id not in module_aliases:
                        module_aliases[tgt.id] = module_aliases[val.id]
                        changed = True
                    elif isinstance(tgt, ast.Attribute) and tgt.attr not in alias_attrs:
                        alias_attrs.add(tgt.attr)
                        changed = True
                elif (isinstance(val, ast.Attribute)
                      and isinstance(val.value, ast.Name)
                      and val.value.id in module_aliases
                      and val.attr in _SUBPROCESS_APIS
                      and isinstance(tgt, ast.Name)
                      and tgt.id not in bare_names):
                    bare_names[tgt.id] = (module_aliases[val.value.id], val.attr)
                    changed = True
                elif (isinstance(val, ast.Name) and val.id in bare_names
                      and isinstance(tgt, ast.Name) and tgt.id not in bare_names):
                    bare_names[tgt.id] = bare_names[val.id]
                    changed = True
    return module_aliases, bare_names, alias_attrs


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
_DIFF_CAPABLE_GIT = {"diff", "show", "log", "format-patch", "range-diff",
                     "whatchanged", "diff-tree", "diff-index", "diff-files",
                     "cherry", "apply"}


def _is_provably_non_diff(tokens, opaque, mutated=False):
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
        verbs = [t for t in tokens[1:] if t and not t.startswith("-")][:2]
        return tuple(verbs) not in _DIFF_CAPABLE_GH
    if sub not in _DIFF_CAPABLE_GIT:
        # 下位コマンドが patch を産まないなら、後から足された引数が何であれ
        # patch にはならない (argv += ["--repo", x] 等は安全)。
        return True
    if mutated:
        # 差分を産みうる下位コマンドに、後から足される引数がある。その中身が
        # -p かもしれないので通さない。
        return False
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


def _mutated_names(fn):
    """``argv.append(...)`` / ``argv += [...]`` のように後から中身を変えられた名前。

    空リストを代入してから足していく形は、代入だけ見ると ``[]`` に見えるため
    「中身を確かめた」と誤認する (独立 judge が実測した穴)。変えられた名前は
    解決済みとして扱わない。
    """
    names = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("append", "extend", "insert", "__iadd__")
                and isinstance(node.func.value, ast.Name)):
            names.add(node.func.value.id)
    return names


def _invocation_sites(tree):
    """``[(関数名, tokens, opaque)]`` — git/gh を走らせうる箇所。

    ``subprocess.run(...)`` と、argv を外から受ける汎用ランナー
    (``_git_ok`` 等) の呼び出しの両方を見る。後者を見ないと
    ``_git_ok(["git", "diff", ...])`` で採る形が素通りする。

    呼び出し先が実行 API かどうかは **属性名だけでなく束縛元** で決める
    (:func:`_runner_bindings` を参照)。名前だけで決めると、別名 import した
    裸の名前を見落とし、同時に無関係な ``.run()`` で偽の赤を出す。
    """
    module_aliases, bare_names, alias_attrs = _runner_bindings(tree)
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
        if (isinstance(node.func, ast.Attribute)
                and node.func.attr in _SUBPROCESS_APIS):
            # ``subprocess.run`` / ``_os.system``。受け手が subprocess / os を
            # 指す名前のときだけ実行 API と認める。``pipeline.run`` は対象外。
            recv = node.func.value
            is_run = ((isinstance(recv, ast.Name) and recv.id in module_aliases)
                      or (isinstance(recv, ast.Attribute)
                          and recv.attr in alias_attrs))
        elif isinstance(node.func, ast.Name) and node.func.id in bare_names:
            is_run = True     # ``from subprocess import run as _r`` → ``_r(...)``
        else:
            is_run = False
        is_runner = isinstance(node.func, ast.Name) and node.func.id in _GENERIC_RUNNERS
        if not (is_run or is_runner):
            continue
        fname = _enclosing_function(parents, node)
        if is_run and fname in _GENERIC_RUNNERS:
            continue          # 汎用ランナー本体は素通し (呼び出し側で見る)
        arg = node.args[0]
        if isinstance(arg, ast.Name) and fname in fn_nodes:
            assigns = _list_assignments(fn_nodes[fname])
            resolved = assigns.get(arg.id)
            mutated = arg.id in _mutated_names(fn_nodes[fname])
            # 空リストから append で組む形は「中身を確かめた」ことにならない
            # (独立 judge が実測した穴)。土台が空なら解決扱いにしない。
            if resolved and all(toks for toks in resolved):
                for toks in resolved:
                    sites.append((fname, toks, False, mutated))
                continue
        toks, opaque = _argv_tokens(arg)
        sites.append((fname, toks, opaque, False))
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
        for fn, toks, opaque, mut in sites
        if fn != _CHOKEPOINT and not _is_provably_non_diff(toks, opaque, mut)
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


# --- 5. 組み立ての境界で落ちる (経路に依存しない最後の関門) --------------------
#
# 独立 AX レビュー (2026-11-16) が、採る側を静的検査で守る形の限界を実測した:
# subprocess.check_output 等の別 API / 空リストから append で組む argv /
# git diff-tree -p —— いずれも検査が緑のまま、未加工の .beacon/project.json が
# judge に渡った。静的検査は有限列挙なので、新しい書き方が出るたびに穴が開く。
#
# bundle は必ず assemble_review_context を通る。そこで落とせば **どう採ったかに
# 依存しない**。以下はその性質を、収集経路を一切使わずに直接確かめる。

def _bundle(artifact, gaps=None):
    return rs.assemble_review_context(
        "ax", origin_id="o", origin_content="原典", diff_text="",
        mode="diff", target_ref="o/r#1", gaps=gaps,
        known_judge_types={"ax"}, artifact=artifact)


def test_unstripped_diff_artifact_is_cleaned_at_assembly():
    """採る側を一切通さず組み立てても、作業記録は judge に渡らない。"""
    b = _bundle({"kind": "diff", "ref": "o/r#1", "content": CODE + LEDGER})
    content = b["artifact"]["content"]
    assert "engine/run.py" in content
    assert ".beacon/project.json" not in content
    assert any(".beacon" in g for g in b["gaps"]), b["gaps"]


def test_unstripped_whole_target_artifact_is_cleaned_at_assembly():
    b = _bundle({"kind": "whole-target", "commits": [
        {"hash": "a1", "diff": CODE + LEDGER},
        {"hash": "b2", "diff": LEDGER},
    ]})
    by = {c["hash"]: c for c in b["artifact"]["commits"]}
    assert ".beacon/project.json" not in by["a1"]["diff"]
    assert "engine/run.py" in by["a1"]["diff"]
    assert by["b2"]["diff"].strip() == ""
    assert any(".beacon" in g for g in b["gaps"]), b["gaps"]


def test_assembly_is_idempotent_and_silent_when_nothing_to_drop():
    """採る側が既に落としていれば no-op。常に出る注記は読まれなくなる。"""
    b = _bundle({"kind": "diff", "ref": "o/r#1", "content": CODE})
    assert b["artifact"]["content"] == CODE
    assert not any(".beacon" in g for g in b["gaps"]), b["gaps"]
    # 二度通しても変わらない
    again, dropped = rs.strip_artifact_bookkeeping(b["artifact"])
    assert again["content"] == CODE and dropped == []


def test_surface_snapshot_artifact_is_left_alone():
    """差分でない artifact (全体棚卸し) は触らない。"""
    art = {"kind": "surface-snapshot", "commands": [{"argv": "beacon --help"}]}
    b = _bundle(art)
    assert b["artifact"] == art


# --- 6. 兄弟の組み立て口も同じ関門を通る -------------------------------------
#
# 独立保守性レビュー (2026-11-16): 落とす処理を assemble_review_context の中に
# 直接置いたら、同じ独立性の契約を運ぶ兄弟 assemble_attainment_context が
# 防御を持たない状態になった (judge が実測)。1 箇所を直して「全域が閉じた」と
# 名乗る型。judge に渡る artifact を作る関数は **全部** 同じ仕上げ口を通す。

def test_attainment_bundle_is_cleaned_at_assembly():
    b = rs.assemble_attainment_context(
        target_id="ms-T", spec_origin_id="s", spec_content="SPEC",
        criteria={"acceptance": "x"}, target_ref="ms-T",
        diff_text=CODE + LEDGER)
    content = b["artifact"]["content"]
    assert "engine/run.py" in content
    assert ".beacon/project.json" not in content, "目的達成の束に作業記録が残っている"
    assert any(".beacon" in g for g in b["gaps"]), b["gaps"]


def test_attainment_bundle_is_silent_when_nothing_to_drop():
    b = rs.assemble_attainment_context(
        target_id="ms-T", spec_origin_id="s", spec_content="SPEC",
        criteria={"acceptance": "x"}, target_ref="ms-T", diff_text=CODE)
    assert b["artifact"]["content"] == CODE
    assert not any(".beacon" in g for g in b["gaps"]), b["gaps"]


def test_every_judge_bundle_builder_goes_through_the_finishing_gate():
    """judge に渡る束を組む関数が、全部 judge_artifact を通っていること。

    三つ目の ``assemble_*_context`` が増えたとき、仕上げ口を通し忘れたら赤くする。
    """
    builders = [n for n in dir(rs)
                if n.startswith("assemble_") and n.endswith("_context")]
    assert len(builders) >= 2, builders
    missing = [n for n in builders
               if "judge_artifact" not in inspect.getsource(getattr(rs, n))]
    assert not missing, (
        f"judge に渡る束を組むのに仕上げ口を通っていない: {missing}。"
        f"judge_artifact を通す (そこで作業記録が落ちる)")


# --- 7. 非 ASCII 名の作業記録も落ちる ----------------------------------------
#
# 独立 AX レビュー (2026-11-16): git は core.quotePath 既定 true のもとで、
# 非 ASCII を含む path をダブルクォートで包み 8 進エスケープして出す。
# 素の文字列として扱うと先頭が " なので a/ の判定が外れ、prefix 一致が起きない。
# judge が .beacon/日本語.json を含む repo を実際に作り、**本文が注記ゼロで
# judge に渡る**ことを実測した。日本語のファイル名はこのプロジェクトで現実的。

QUOTED_LEDGER = (
    'diff --git "a/.beacon/\\346\\227\\245.json" "b/.beacon/\\346\\227\\245.json"\n'
    '--- "a/.beacon/\\346\\227\\245.json"\n'
    '+++ "b/.beacon/\\346\\227\\245.json"\n'
    '@@ -1 +1 @@\n-{"summary": "前回の経緯"}\n+{"summary": "今回の経緯"}\n'
)


def test_quoted_non_ascii_bookkeeping_path_is_dropped():
    out, dropped = rs.strip_bookkeeping_from_diff(CODE + QUOTED_LEDGER)
    assert "engine/run.py" in out
    assert "前回の経緯" not in out, "非 ASCII 名の作業記録が judge に渡っている"
    assert dropped, "落としたのに記録されていない"
    assert any(".beacon/" in path for path, _ in dropped), dropped


def test_quoted_path_is_decoded_back_to_its_real_name():
    assert rs.unquote_git_path('"a/.beacon/\\346\\227\\245.json"') == "a/.beacon/日.json"
    # 引用符が無ければそのまま (ASCII path の既存挙動を変えない)
    assert rs.unquote_git_path("a/engine/run.py") == "a/engine/run.py"


def test_quoted_non_ascii_is_also_dropped_at_the_assembly_boundary():
    b = _bundle({"kind": "diff", "ref": "o/r#1", "content": CODE + QUOTED_LEDGER})
    assert "前回の経緯" not in b["artifact"]["content"]
    assert any(".beacon" in g for g in b["gaps"]), b["gaps"]


def test_gh_call_with_diff_as_an_argument_value_is_not_flagged():
    """`gh issue list --label diff` は差分を産まない。偽の赤を出さない。"""
    assert _is_provably_non_diff(["gh", "issue", "list", "--label", "diff"], False)
    # 本物の差分口は拒否されたまま
    assert not _is_provably_non_diff(["gh", "pr", "diff", "123"], False)


# --- 8. 呼び出し先の素性を見る (名前だけで判定しない) ------------------------
#
# 独立 AX レビュー (2026-11-16) が、属性名だけを見る述語の両方向の誤りを実測した。
# 見落とし (別名 import の裸の名前) と偽の赤 (無関係な .run()) は 1 つの修正で
# 閉じる: 呼び出し先の束縛元を import 文から解決する (e-7519)。

def _offenders_in(src):
    """合成ソースを検査 B と同じ述語に通し、offender の関数名を返す。"""
    sites = _invocation_sites(ast.parse(src))
    return [fn for fn, toks, opaque, mut in sites
            if fn != _CHOKEPOINT and not _is_provably_non_diff(toks, opaque, mut)]


def test_aliased_bare_import_of_a_runner_is_examined():
    """``from subprocess import run as _r`` 経由で採る形を見落とさない。"""
    assert _offenders_in(
        "from subprocess import run as _r\n"
        "def collect(ref):\n"
        "    return _r(['git', 'diff', '-U25', ref]).stdout\n"
    ) == ["collect"]
    # 関数の中で import しても同じ (os.system で採る既存の形と揃える)
    assert _offenders_in(
        "def collect(ref):\n"
        "    from subprocess import check_output as _co\n"
        "    return _co(['git', 'diff', '-U25', ref])\n"
    ) == ["collect"]


def test_run_method_on_an_unrelated_object_is_not_flagged():
    """差分と無関係なオブジェクトの ``.run()`` で赤くならない。

    ``run`` はありふれた名前なので、偽の赤が出ると踏んだ人が述語を緩める。
    """
    assert _offenders_in(
        "import subprocess\n"
        "class Pipeline:\n"
        "    def run(self, payload):\n"
        "        return payload\n"
        "def drive(pipeline, payload):\n"
        "    return pipeline.run(payload)\n"
    ) == []
    # subprocess を指す名前への呼び出しは、別名でも拒否されたまま
    assert _offenders_in(
        "import subprocess as sp\n"
        "def collect(ref):\n"
        "    return sp.run(['git', 'diff', ref]).stdout\n"
    ) == ["collect"]


def test_import_of_a_non_running_name_is_not_treated_as_a_runner():
    """``from subprocess import PIPE`` は実行の口ではない。"""
    assert _offenders_in(
        "from subprocess import PIPE\n"
        "def shape(x):\n"
        "    return PIPE(x)\n"
    ) == []


# --- 9. ヘッダーを持たないセクションの path 割り出し (e-7520) -----------------
#
# 100% 一致の rename / mode のみの変更では ``---`` / ``+++`` が出ず、
# ``diff --git a/X b/Y`` の 1 行から 2 つの path を割り出すしかない。以前は
# 最初の ``" b/"`` で割っており、X 自身がそれを含むと割り位置を誤った
# (独立保守性レビュー 2026-11-16)。下の 3 本は実際の git 出力と同じ形
# (``weird b/`` というディレクトリを作って git mv した結果を採取した)。

# 100% 一致の rename。git は ---/+++ を出さず、rename from/to だけを出す。
TRICKY_RENAME = (
    "diff --git a/weird b/case.txt b/.beacon/thing.txt\n"
    "similarity index 100%\n"
    "rename from weird b/case.txt\n"
    "rename to .beacon/thing.txt\n"
)
PLAIN_RENAME = (
    "diff --git a/old.txt b/.beacon/moved.txt\n"
    "similarity index 100%\n"
    "rename from old.txt\n"
    "rename to .beacon/moved.txt\n"
)
# mode のみの変更。両側が同じ path で、名前に ``" b/"`` を含む。
TRICKY_MODE_ONLY = (
    "diff --git a/weird b/case.txt b/weird b/case.txt\n"
    "old mode 100644\n"
    "new mode 100755\n"
)


def test_headerless_rename_from_a_confusing_name_is_dropped():
    """名前に ``" b/"`` を含むファイルから作業記録の配下へ移しても落ちる。"""
    out, dropped = rs.strip_bookkeeping_from_diff(CODE + TRICKY_RENAME)
    assert "engine/run.py" in out
    assert ".beacon/thing.txt" not in out, "作業記録への rename が judge に渡っている"
    assert [p for p, _ in dropped] == [".beacon/thing.txt"], dropped


def test_headerless_rename_paths_are_split_correctly():
    assert rs._diff_section_paths(TRICKY_RENAME.splitlines(keepends=True)) == [
        "weird b/case.txt", ".beacon/thing.txt"]
    # 空白を含まない普通の rename は今までどおり
    assert rs._diff_section_paths(PLAIN_RENAME.splitlines(keepends=True)) == [
        "old.txt", ".beacon/moved.txt"]


def test_headerless_mode_change_keeps_both_sides_on_the_same_path():
    """rename でないセクションは両側が同じ path。紛らわしい名前でも割り誤らない。"""
    assert rs._diff_section_paths(TRICKY_MODE_ONLY.splitlines(keepends=True)) == [
        "weird b/case.txt", "weird b/case.txt"]
    # 作業記録ではないので落ちない (偽の drop を出さない)
    out, dropped = rs.strip_bookkeeping_from_diff(TRICKY_MODE_ONLY)
    assert out == TRICKY_MODE_ONLY and dropped == []


# --- 10. 代入で作った別名も追う (PR #850 独立 AX、実測で 3 形が素通りした) -------

@pytest.mark.parametrize("src", [
    # モジュールを別の名前へ代入し直す (変更前は拒否側に倒れていた)
    "import subprocess\n"
    "sp = subprocess\n"
    "def collect(ref):\n"
    "    return sp.run(['git', 'diff', ref]).stdout\n",
    # 属性に持たせ、別の受け手から呼ぶ
    "import subprocess\n"
    "class C:\n"
    "    def __init__(self):\n"
    "        self.p = subprocess\n"
    "def collect(s, ref):\n"
    "    return s.p.run(['git', 'diff', ref]).stdout\n",
    # 実行関数そのものを束縛する
    "import subprocess\n"
    "f = subprocess.run\n"
    "def collect(ref):\n"
    "    return f(['git', 'diff', ref]).stdout\n",
    # 代入の連鎖
    "import subprocess\n"
    "a = subprocess\n"
    "b = a\n"
    "def collect(ref):\n"
    "    return b.check_output(['git', 'diff', ref])\n",
])
def test_rebound_runner_names_are_examined(src):
    assert _offenders_in(src) == ["collect"], src


def test_a_relative_import_named_subprocess_is_not_a_runner():
    """``from .subprocess import run`` は標準の subprocess ではない (node.level ガード)。"""
    assert _offenders_in(
        "from .subprocess import run\n"
        "def drive(payload):\n"
        "    return run(payload)\n"
    ) == []


# --- 11. ヘッダー無しセクションの残りの枝 (PR #850 独立保守性: 6 変異が生き残った) --

def test_headerless_copy_reads_copy_lines():
    sec = ("diff --git a/weird b/src.txt b/.beacon/copy.txt\n"
           "similarity index 100%\n"
           "copy from weird b/src.txt\n"
           "copy to .beacon/copy.txt\n")
    assert rs._diff_section_paths(sec.splitlines(keepends=True)) == [
        "weird b/src.txt", ".beacon/copy.txt"]


def test_headerless_quoted_rename_is_unquoted_and_unprefixed():
    sec = ('diff --git "a/caf\\303\\251.txt" "b/.beacon/caf\\303\\251.txt"\n'
           "similarity index 100%\n"
           'rename from "caf\\303\\251.txt"\n'
           'rename to ".beacon/caf\\303\\251.txt"\n')
    assert rs._diff_section_paths(sec.splitlines(keepends=True)) == [
        "café.txt", ".beacon/café.txt"]


def test_headerless_plain_mode_change_strips_the_ab_prefix():
    """空白の無い 2 トークンの枝は a/ b/ を外す (_strip_ab_prefix を外す変異を捕まえる)。"""
    sec = ("diff --git a/x.py b/x.py\n"
           "old mode 100644\n"
           "new mode 100755\n")
    assert rs._diff_section_paths(sec.splitlines(keepends=True)) == ["x.py", "x.py"]


def test_a_single_side_line_is_not_enough():
    """rename 行が片方しか無い壊れた形は、diff --git 行の割り出しに回る (>= 2 の境界)。"""
    sec = ("diff --git a/old.txt b/.beacon/new.txt\n"
           "rename from old.txt\n")
    assert rs._diff_section_paths(sec.splitlines(keepends=True)) == [
        "old.txt", ".beacon/new.txt"]


def test_undecidable_split_returns_every_candidate():
    """両側が違い rename 行も無い形は、割り位置を決めず候補を全部返す (落とす側なので多めが安全)。"""
    sec = "diff --git a/p b/q b/.beacon/r\n"
    got = rs._diff_section_paths([sec])
    assert set(got) == {"p", "q b/.beacon/r", "p b/q", ".beacon/r"}, got


def test_a_line_not_starting_with_a_slash_yields_nothing():
    """``a/`` で始まらない (= git の出力でない) 行からは推測で path を作らない。

    ``" b/"`` を含む入力にしないとガードの有無で結果が変わらない (最初に書いた
    ``x y z`` はガードを外しても [] のままで、この試験自体が偽の緑だった)。
    """
    assert rs._diff_section_paths(["diff --git x b/y b/z\n"]) == []


# --- 12. batch の原典解決は repo-file に限る (PR #850 独立保守性) -------------

def test_batch_origin_rejects_non_repo_file_kinds(capsys):
    import commands
    with pytest.raises(SystemExit):
        commands._origin_for_batch_descriptor(
            {"id": "x", "origin": {"kind": "doc"}}, [])
    assert "repo-file" in capsys.readouterr().err


# --- 13. os 側・名前の連鎖・実行しない名前の代入 (PR #850 独立保守性 2 周目) --
#
# 挙動は正しかったが、壊しても試験が赤くならなかった枝 (os の束縛 / from os
# import / 裸の名前の代入連鎖 / 代入元を実行 API に絞る条件)。

@pytest.mark.parametrize("src", [
    "import os as _os\n"
    "def collect(ref):\n"
    "    return _os.system('git diff ' + ref)\n",
    "from os import system as s\n"
    "def collect(ref):\n"
    "    return s('git diff ' + ref)\n",
    "import subprocess\n"
    "f = subprocess.run\n"
    "g = f\n"
    "def collect(ref):\n"
    "    return g(['git', 'diff', ref]).stdout\n",
])
def test_os_and_chained_runner_names_are_examined(src):
    assert _offenders_in(src) == ["collect"], src


def test_binding_a_non_running_name_is_not_a_runner():
    """``f = subprocess.PIPE`` の後の ``f(x)`` は実行の口ではない (偽の赤を出さない)。"""
    assert _offenders_in(
        "import subprocess\n"
        "f = subprocess.PIPE\n"
        "def shape(x):\n"
        "    return f(x)\n"
    ) == []
