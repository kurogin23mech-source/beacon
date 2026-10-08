#!/usr/bin/env python3
"""server/ が lib/ を sys.path に載せる箇所を 1 つに保つ (ms-166 e-6728)。

`server/_libpath.py` の docstring は自分を「lib/ への道を知る唯一の持ち主」と
名乗る。だが名乗りは守らない。2026-10-08 の独立レビューが実際にこれを捕まえた:
`_libpath` を作って app.py と firestore_client.py を揃えた直後の時点で、
`server/routers_projects.py` の検索ハンドラの中に **3 つ目のコピー**
(``_LIB = os.path.join(dirname(__file__), "..", "lib")`` → ``sys.path.insert``)
が関数の中に残っていた。`grep _libpath` では出てこないので、次に lib/ の相対位置を
変える人・god-module を分割する人は、その 1 箇所だけ取り残す。

**宣言ではなく機械で閉じる。** 構文木で「sys.path に載せる呼び出し」を拾い、
載せる対象が lib/ を指すものだけを lib-path 挿入と数える。許される場所は
`_libpath.py` だけ。

意図的に lib/ 以外を載せる箇所 (例: server/ 自身を載せる lambda_handler.py や
create_*_tables.py) は対象外 — 守りたい状態は「lib/ への道の持ち主が 1 つ」なので、
別の道を数えると守る対象がぼやける。

実行: python3 scripts/check-lib-path-single-owner.py [--strict]
  見つかったら stderr に場所を並べて exit 1 (--strict でも既定でも同じ)。
pytest に依存しない素のスクリプト (lint-docs から呼ばれるため)。
"""
from __future__ import annotations

import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_DIR = os.path.join(ROOT, "server")

# lib/ への道を知ってよい唯一のファイル。
OWNER = "_libpath.py"


def _string_constants(node: ast.AST) -> set[str]:
    """部分木に現れる文字列リテラルを集める。"""
    out: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            out.add(sub.value)
    return out


def _targets_lib(strings: set[str]) -> bool:
    """集めた文字列が lib/ ディレクトリを指しているか。"""
    for s in strings:
        norm = s.replace("\\", "/").rstrip("/")
        if norm == "lib" or norm.endswith("/lib"):
            return True
    return False


def _name_to_strings(tree: ast.AST) -> dict[str, set[str]]:
    """``_LIB = os.path.join(..., "lib")`` 形の局所変数を文字列集合に解く。

    挿入の引数が変数名のときに辿るための対応表。スコープは区別せず名前だけで引く
    (同名の別物を取り違える可能性より、辿れずに見落とす方が害が大きい)。
    """
    table: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            strings = _string_constants(value)
            if not strings:
                continue
            for t in targets:
                if isinstance(t, ast.Name):
                    table.setdefault(t.id, set()).update(strings)
    return table


def _is_sys_path_mutation(call: ast.Call) -> bool:
    """``<anything>.path.insert(...)`` / ``.path.append(...)`` か。

    ``sys`` は ``import sys as _sys`` で別名になりうるので、名前ではなく
    ``.path.<insert|append>`` という形で判定する。
    """
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr not in ("insert", "append"):
        return False
    owner = func.value
    return isinstance(owner, ast.Attribute) and owner.attr == "path"


def find_lib_path_inserts() -> list[tuple[str, int, str]]:
    findings: list[tuple[str, int, str]] = []
    for fname in sorted(os.listdir(SERVER_DIR)):
        if not fname.endswith(".py") or fname == OWNER:
            continue
        path = os.path.join(SERVER_DIR, fname)
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        try:
            tree = ast.parse(src, filename=path)
        except SyntaxError as exc:  # 構文エラーは別のガードの仕事
            findings.append((fname, getattr(exc, "lineno", 0) or 0, f"構文解析できない: {exc}"))
            continue
        names = _name_to_strings(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not _is_sys_path_mutation(node):
                continue
            strings = set()
            for arg in node.args:
                strings |= _string_constants(arg)
                if isinstance(arg, ast.Name):
                    strings |= names.get(arg.id, set())
            if _targets_lib(strings):
                findings.append((fname, node.lineno, ast.unparse(node)))
    return findings


def main(argv: list[str]) -> int:
    findings = find_lib_path_inserts()
    if not findings:
        print(
            f"[check-lib-path-single-owner] OK: lib/ を sys.path に載せるのは "
            f"server/{OWNER} だけ (server/*.py を構文木で検査)。"
        )
        return 0

    print(
        f"[check-lib-path-single-owner] NG: lib/ への道を知る箇所が "
        f"server/{OWNER} の外に {len(findings)} 件あります。",
        file=sys.stderr,
    )
    for fname, lineno, snippet in findings:
        print(f"  server/{fname}:{lineno}  {snippet}", file=sys.stderr)
    print(
        f"\n  直し方: その行を削り、ファイル冒頭 (最初の lib/ import より上) で\n"
        f"  `import _libpath` に置き換えてください。知識の写しが増えると、\n"
        f"  lib/ の位置を変えたときに 1 箇所だけ取り残されます。",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
