#!/usr/bin/env python3
"""git の worktree 判定を立てている箇所を台帳と突き合わせる (ms-166 e-6932)。

`beacon_cli/hooks/context_monitor.py::_worktree_shared_base` の docstring は
「この問いを立てている箇所は 3 つで、git の答えの形が変わったら 3 つが変更対象の
集合」と **名乗る**。だが名乗りは守らない。2026-10-10 の独立レビュー (保守性) が
これを指摘した: repo は `check-pid-liveness.py` で同種の約束を機械化できることを
既に実証しているのに、この約束は散文のままだった。

**宣言ではなく機械で閉じる。** 構文木で「``git rev-parse`` に ``--git-dir`` /
``--git-common-dir`` を渡す呼び出し」を拾い、台帳 (下の CENSUS) と集合比較する。
増えていれば「台帳に足して 3 つの docstring も直せ」と言い、減っていれば
「台帳から外せ」と言う。どちらも黙って通さない。

なぜ共有ヘルパーに寄せないか: 3 箇所のうち `context_monitor.py` は **lib/ を
import せずに動く**制約を持つフック (Claude Code の hook として、beacon の
インストール状態に依らず起動する)。共有ヘルパーに寄せるとその制約を破る。だから
「1 箇所に集約する」のではなく「散在を機械で数え、増減を検知する」側で閉じた。

**この台帳は許可リストではなく人口調査 (census)。** 「ここは例外」と免除するのでは
なく、「全部で何箇所あるか」を固定する。免除リストは狭すぎて破れるが (出会った実例
しか免除しない)、人口調査は増減の両方で赤くなる。原則は CORE doc
`jnFsoNaNvuZegI73noCn` (免除とガードは「性質」で閉じる — 狭すぎても緩すぎても破れる)。

対象外: `tests/` 配下。テストは git 応答を差し替える stub としてこの旗を書くので、
数えると守りたい状態 (= 本番コードで何箇所が git にこの問いを立てるか) がぼやける。

実行: python3 scripts/check-worktree-probe-census.py [--strict]
  差分があれば stderr に並べて exit 1 (--strict でも既定でも同じ)。
pytest に依存しない素のスクリプト (lint-docs から呼ばれるため)。
"""
from __future__ import annotations

import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# git に worktree の素性を尋ねる旗。どちらかが渡っていれば「この問いを立てた」と数える。
PROBE_FLAGS = frozenset({"--git-dir", "--git-common-dir"})

# 走査する範囲 (tests/ は上記の理由で除外、.trash / .git も当然除外)。
SCAN_DIRS = ("beacon_cli", "lib", "scripts", "server", "bin")

# 台帳: 本番コードでこの問いを立てている (ファイル, 関数) の全体。
#
# **このガードは初回実行で、宣言が間違っていたことを見つけた。** 当時
# `context_monitor.py` の docstring は「この問いを立てているのは 3 箇所」と
# 名乗っていたが、実際は 4 箇所あった — `lib/cmd_milestone.py::_is_git_project`
# が `--git-dir` だけを使って「ここは git repo か」を尋ねており、docstring 自身も
# "covers the worktree case" と書いている。`--git-common-dir` だけを grep すると
# 見落とす形で、人が数えると落ちる 4 人目だった。
#
# 増やすとき / 減らすときは、集合を名指ししている docstring
# (`context_monitor.py::_worktree_shared_base`) も直すこと。他の 3 箇所は件数を
# 宣言していないので、直す対象はその 1 つ。
CENSUS = frozenset({
    ("beacon_cli/hooks/context_monitor.py", "_worktree_shared_base"),
    ("scripts/check-branch-focus-divergence.py", "is_in_main_project_root"),
    ("lib/cmd_milestone.py", "_is_in_main_project_root"),
    # 「worktree か」ではなく「git repo か」を尋ねるが、消費する旗が同じ
    # (`--git-dir`) なので、git の答えの形が変わると一緒に影響を受ける。
    ("lib/cmd_milestone.py", "_is_git_project"),
})


def _string_constants(node: ast.AST) -> set:
    """部分木に現れる文字列リテラルを集める。"""
    out = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            out.add(sub.value)
    return out


def _is_probe_call(call: ast.Call) -> bool:
    """``git rev-parse --git-dir`` / ``--git-common-dir`` を渡す呼び出しか。

    呼び出し先の名前 (subprocess.run / check_output / _run / 自作ラッパ) では
    判定しない — 3 箇所がそれぞれ別のラッパを使っており、名前で数えると
    ラッパを増やした人が黙ってすり抜ける。**渡している引数の中身**で数える。
    """
    strings = set()
    for arg in list(call.args) + [kw.value for kw in call.keywords]:
        strings |= _string_constants(arg)
    return ("git" in strings and "rev-parse" in strings
            and bool(strings & PROBE_FLAGS))


def _enclosing_function(tree: ast.AST, target: ast.AST) -> str:
    """``target`` を含む最も内側の関数名 (無ければ ``<module>``)。"""
    best = "<module>"
    best_span = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = getattr(node, "end_lineno", None)
        if end is None or not (node.lineno <= target.lineno <= end):
            continue
        span = end - node.lineno
        if best_span is None or span < best_span:
            best, best_span = node.name, span
    return best


def find_probe_sites() -> set:
    """本番コードで worktree 判定を立てている (相対パス, 関数名) の集合。"""
    found = set()
    for top in SCAN_DIRS:
        base = os.path.join(ROOT, top)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames
                           if d not in ("__pycache__", ".trash", "node_modules")]
            for fname in sorted(filenames):
                if not fname.endswith(".py"):
                    continue
                path = os.path.join(dirpath, fname)
                rel = os.path.relpath(path, ROOT).replace("\\", "/")
                try:
                    with open(path, encoding="utf-8") as fh:
                        tree = ast.parse(fh.read(), filename=path)
                except (SyntaxError, UnicodeDecodeError):
                    continue  # 構文エラーは別のガードの仕事
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call) and _is_probe_call(node):
                        found.add((rel, _enclosing_function(tree, node)))
    return found


def main(argv) -> int:
    found = find_probe_sites()
    added = sorted(found - CENSUS)
    removed = sorted(CENSUS - found)
    if not added and not removed:
        print(
            "[check-worktree-probe-census] OK: git に worktree の素性を尋ねるのは "
            f"台帳どおり {len(CENSUS)} 箇所 (本番コードを構文木で検査、tests/ は対象外)。"
        )
        return 0

    print(
        "[check-worktree-probe-census] NG: worktree 判定の箇所が台帳と違います。",
        file=sys.stderr,
    )
    for rel, func in added:
        print(f"  + 台帳に無い: {rel}::{func}", file=sys.stderr)
    for rel, func in removed:
        print(f"  - 台帳にあるが見つからない: {rel}::{func}", file=sys.stderr)
    print(
        "\n  直し方: このスクリプトの CENSUS を実態に合わせ、**あわせて 3 箇所の\n"
        "  docstring が参照し合っている集合も直す** (互いを名指ししているので、\n"
        "  1 箇所だけ足すと残りの説明が古くなる)。git の答えの形を変える変更は、\n"
        "  この集合の全員を同時に見る必要があります。",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
