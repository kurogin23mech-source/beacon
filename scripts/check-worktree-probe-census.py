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

import argparse
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


# 件数を宣言している説明文 (= ドリフト時に直す対象)。案内文はここから組み立てる。
# prose に「3 箇所の docstring」と書いていた最初の版は、同じファイルの CENSUS 注記
# (「件数を宣言しているのは 1 つだけ」) と矛盾しており、CI で落ちた人に存在しない
# 宣言を探させる形だった (AX レビュー PR #791、misleading)。
DECLARES_THE_COUNT = frozenset({
    ("beacon_cli/hooks/context_monitor.py", "_worktree_shared_base"),
})


def _string_constants(node: ast.AST) -> set:
    """部分木に現れる文字列リテラルを集める。"""
    out = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            out.add(sub.value)
    return out


def _name_to_strings(tree: ast.AST) -> dict:
    """``args = ["git", "rev-parse", "--git-dir"]`` 形の局所変数を文字列集合に解く。

    **これを落としていたのが、このガードの最初の版の穴だった** (保守性レビュー
    PR #791、high)。リテラルを直接渡す形だけを数えていたので、ごく普通の整理
    (引数リストを変数に出す) で検知対象が黙って消え、ガードは「台帳どおり」と
    緑を出し続けた。*宣言が黙って古くなるのを止めるための道具自身が、黙って古く
    なる* 形だった。

    姉妹ガード ``check-lib-path-single-owner.py`` は同型の間接参照をまさに
    ``_name_to_strings`` で解いており、そこから ``_string_constants`` を写した
    のに、隣にあるこの一手だけ落としていた。

    スコープは区別せず名前だけで引く (同名の別物を取り違える可能性より、辿れずに
    見落とす方が害が大きい — 姉妹ガードと同じ非対称)。
    """
    table: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            strings = _literal_strings(value)
            if not strings:
                continue
            for t in targets:
                if isinstance(t, ast.Name):
                    table.setdefault(t.id, set()).update(strings)
    return table


def _literal_strings(value: ast.AST) -> set:
    """``value`` が **文字列リテラルの入れ物** のときだけ、その文字列を返す。

    対象は ``"..."`` と ``[...]`` / ``(...)`` / ``{...}`` の要素が文字列リテラルの形
    だけ。部分木を無差別に歩いて文字列を拾ってはならない。

    **無差別に拾う版を最初に書いて、実測で誤検知を出した**: ``r = subprocess.run(
    ["git", "rev-parse", "--git-dir"], ...)`` のような *結果を受ける変数* まで
    「旗を持つ」と記録され、名前解決がスコープを区別しないため、別の関数の同名変数
    (``r`` / ``gd`` / ``cd``) がそれを引き込んで無関係な行を数えた
    (``cmd_milestone_list`` が誤検知された)。

    姉妹ガード ``check-lib-path-single-owner.py`` の ``_name_to_strings`` は
    ``_LIB = os.path.join(dirname(__file__), "..", "lib")`` という *呼び出し式から
    文字列を拾いたい* 用途なので無差別で正しい。ここは用途が逆 — 写すときに
    「なぜその形なのか」まで持ってこないと、同じコードが別の意味で間違う。
    """
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return {value.value}
    if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
        return {el.value for el in value.elts
                if isinstance(el, ast.Constant) and isinstance(el.value, str)}
    return set()


def _tokens(strings: set) -> set:
    """文字列集合を「語」の集合にほどく。

    リスト形 (``["git", "rev-parse", "--git-dir"]``) と シェル形
    (``"git rev-parse --git-dir"`` を ``shell=True`` で渡す) の両方を同じ土俵で
    見るため。部分一致 (``"git" in blob``) にすると ``digit`` 等で誤検知するので、
    空白で割った語として突き合わせる。
    """
    out = set()
    for s in strings:
        out.add(s)
        out |= {tok for tok in s.split() if tok}
    return out


def _is_probe_call(call: ast.Call, names: dict) -> bool:
    """``git rev-parse --git-dir`` / ``--git-common-dir`` を渡す呼び出しか。

    呼び出し先の名前 (subprocess.run / check_output / _run / 自作ラッパ) では
    判定しない — 4 箇所がそれぞれ別のラッパを使っており、名前で数えると
    ラッパを増やした人が黙ってすり抜ける。**渡している引数の中身**で数える。

    判定: 語として git / rev-parse / 旗 が揃っていれば数える。局所変数に入れた形は
    ``_name_to_strings`` で解いてから見るので、``args = [...]`` に出す整理では消えない。
    リスト形とシェル形 (``shell=True`` の 1 本の文字列) は ``_tokens`` が同じ土俵に
    乗せる。``rev-parse --is-inside-work-tree`` のような別の問いは数えない。

    **残る限界 (= 緑が主張しないこと)**: 旗を実行時に組み立てる形 (f-string / 文字列
    連結 / 関数の戻り値) は検知しない。最初の修正では「読み切れないなら曖昧側に倒して
    数える」規則を入れたが、実測すると repo 全体の `git rev-parse` 呼び出し 15 件
    (`--short HEAD` など別の問い) を巻き込み、守りたい集合がぼやけた — 狭すぎる側を
    直した反動で緩すぎる側に倒れる形で、これは今日この repo で踏んだのと同じ病気
    (CORE doc `jnFsoNaNvuZegI73noCn`)。今そういう書き方は 1 件も無いので、仮定の形に
    規則を足して無関係な呼び出しを誤って数えるより、**覆っていない範囲を明記する**
    方を選んだ。もしそういう形が生まれたら、その時に台帳へ手で足すことになる。
    """
    strings = set()
    for arg in list(call.args) + [kw.value for kw in call.keywords]:
        strings |= _string_constants(arg)
        for sub in ast.walk(arg):
            if isinstance(sub, ast.Name):
                strings |= names.get(sub.id, set())
    toks = _tokens(strings)
    return ("git" in toks and "rev-parse" in toks and bool(toks & PROBE_FLAGS))


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


def find_probe_sites(root: str = None) -> set:
    """本番コードで worktree 判定を立てている (相対パス, 関数名) の集合。"""
    root = root or ROOT
    found = set()
    for top in SCAN_DIRS:
        base = os.path.join(root, top)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames
                           if d not in ("__pycache__", ".trash", "node_modules")]
            for fname in sorted(filenames):
                if not fname.endswith(".py"):
                    continue
                path = os.path.join(dirpath, fname)
                rel = os.path.relpath(path, root).replace("\\", "/")
                try:
                    with open(path, encoding="utf-8") as fh:
                        tree = ast.parse(fh.read(), filename=path)
                except (SyntaxError, UnicodeDecodeError):
                    continue  # 構文エラーは別のガードの仕事
                names = _name_to_strings(tree)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call) and _is_probe_call(node, names):
                        found.add((rel, _enclosing_function(tree, node)))
    return found


def main() -> int:
    # 姉妹ガード check-pid-liveness.py と同じ argparse の形にする。最初の版は
    # `main(argv)` を取りながら argv を一度も読まず、`--strict` / `--help` / 架空の
    # 旗のどれを渡しても同じ出力を返していた (AX レビュー PR #791、high)。
    # `--strict` は姉妹では exit code を分岐させるので、同名で別の意味を持たせると
    # そちらから学んだ挙動モデルが外れる。意味も揃える: 既定は警告のみ、`--strict`
    # で初めて落ちる (CI の ci-strict-drift-guards.sh は --strict 付きで呼ぶ)。
    parser = argparse.ArgumentParser(
        description="Count the production call sites that ask git about "
                    "--git-dir / --git-common-dir and compare to the census.",
    )
    parser.add_argument(
        "--strict", action="store_true", help="exit 1 on drift (CI gate)"
    )
    parser.add_argument(
        "--root", default=None,
        help="scan this directory instead of the repo root "
             "(used by the guard's own test)",
    )
    args = parser.parse_args()
    root = os.path.abspath(args.root) if args.root else ROOT

    found = find_probe_sites(root)
    added = sorted(found - CENSUS)
    removed = sorted(CENSUS - found)
    if not added and not removed:
        print(
            "[check-worktree-probe-census] OK: git に worktree の素性を尋ねるのは "
            f"台帳どおり {len(CENSUS)} 箇所 (本番コードを構文木で検査、tests/ は対象外)。"
            " 旗を実行時に組み立てる形は見ていない (docstring の『残る限界』参照)。"
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
    # 「どの説明文を直すか」は prose に書かず DECLARES_THE_COUNT から組み立てる。
    # 最初の版は「3 箇所の docstring も直せ」と書いていたが、同じファイルの CENSUS
    # 注記は「件数を宣言しているのは 1 つだけ」と書いており、案内文が自分の台帳と
    # 矛盾していた (AX レビュー PR #791、misleading)。存在しない宣言を探しに行かせる
    # 形だったので、データから出す。
    targets = ", ".join(f"{rel}::{func}" for rel, func in sorted(DECLARES_THE_COUNT))
    print(
        "\n  直し方: このスクリプトの CENSUS を実態に合わせ、**あわせて件数を宣言\n"
        f"  している説明文も直す**: {targets}\n"
        "  (他の箇所は件数を宣言していないので、直す対象はこれだけ。git の答えの形を\n"
        "  変える変更は、台帳の全員を同時に見る必要があります。)",
        file=sys.stderr,
    )
    return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
