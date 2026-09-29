#!/usr/bin/env python3
"""Windows-unsafe pid 生存判定 (= 素の ``os.kill(pid, 0)``) の drift guard
(ms-133 / e-6591)。

なぜこれが要るか
----------------
Python の ``os.kill`` は Windows で signal 0 を含むあらゆる signal を
``TerminateProcess`` に写す。つまり POSIX の定石 ``os.kill(pid, 0)`` は Windows
では「生きてますか」ではなく「死んでください」になる。Beacon はこの問いを
bridge claim / Codex daemon / bcodex watcher / 版ズレ検知 / context-monitor の
5 箇所で持っており、e-6591 の時点で 4 箇所が Windows 利用者のプロセスを
落としていた。

修正 (= 単一 probe ``lib/pid_liveness.py`` への集約) はコードから読めば分かるが、
**次に生存判定を書く人**には見えない。素の os.kill は POSIX では正しく動いて
しまうので、Mac/Linux の開発者・AI は再導入しても気づけない (CI も緑のまま、
Windows 利用者だけが壊れる)。この guard は再導入を機械的に落とす。

検査ルール
----------
1. 追跡対象の ``*.py`` を AST で走査し、``os.kill`` / ``kill``
   (= ``from os import kill``) の呼び出しを列挙する。
   **安全側に倒す**: signal 引数が 0 でないと **静的に証明できる** 呼び出し
   (literal の非 0、``signal.SIGTERM`` 等の本物の signal 名) だけを正当な kill と
   して除外し、**それ以外はすべて検出する**。literal 0 だけを見る検査は
   ``_PROBE = 0`` のような定数化や ``os.kill(*args)`` で素通りしてしまい、
   「ok と出るのに Windows-unsafe な呼び出しが残る」偽の安全を作る
   (2026-09-29 の独立 AX レビューで実証された穴)。誤検出 (false positive) は
   人間が 1 行 allowlist に足せば済むが、検出漏れ (false negative) は Windows
   利用者のプロセスが落ちるまで誰も気づかない — 非対称なので安全側を選ぶ。
2. 見つかった各サイトを ``ALLOWLIST`` (ファイル + 囲む関数) と突き合わせる。
   載っていないものは drift。
3. allowlist は **コメントを信用しない**: ``os.name != "posix"`` gate を根拠に
   許可しているエントリについては、その gate が実際に AST に在ることを確認する。
   gate を消して os.kill だけ残す改変は、allowlist に載っていても落ちる。

実行:
  python3 scripts/check-pid-liveness.py            # warn mode (= pre-commit)
  python3 scripts/check-pid-liveness.py --strict   # exit 1 on drift (= CI gate)
"""
from __future__ import annotations

import argparse
import ast
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# 走査から外すディレクトリ (生成物・依存・別 worktree)。
SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".worktrees",
    ".trash", ".archive", "build", "dist", ".mypy_cache", ".pytest_cache",
}

# ``os.kill(pid, 0)`` を書いてよい唯一の場所たち。
#   key   = (repo 相対パス, 囲む関数名)
#   value = (理由, POSIX gate の検証を要求するか)
# 新しいエントリを足すのは「なぜ単一 probe を使えないか」を説明できるときだけ。
ALLOWLIST: dict[tuple[str, str], tuple[str, bool]] = {
    ("lib/pid_liveness.py", "_pid_alive_posix"): (
        "唯一の正規の置き場所。pid_alive() が os.name で分岐した POSIX 側の実装本体。",
        False,
    ),
    ("beacon_cli/hooks/context_monitor.py", "_pid_alive"): (
        "e-6588 で呼び出し側を POSIX 限定に gate 済み。このフックは pipx の素の "
        "stdlib だけで動く自己完結ファイルという設計制約があり lib/ を import "
        "しないため、単一 probe に寄せず gate を維持する。",
        True,
    ),
}

# 上の ``True`` エントリが要求する gate。呼び出し元の関数が
# ``if os.name != "posix": return`` を持っていることを AST で確認する。
POSIX_GATE_HINT = 'if os.name != "posix": return'


def _iter_python_files() -> list[Path]:
    """追跡対象の ``*.py`` を列挙する (tests/ も含める = テストの再導入も防ぐ)。"""
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name.endswith(".py"):
                out.append(Path(dirpath) / name)
    return sorted(out)


# ``signal`` の定数のうち値が 0 のもの (= 実際には生存判定に使われる)。
# これらは「本物の signal 名だから正当」とは扱わない。
_ZERO_VALUED_SIGNAL_NAMES = {"SIG_DFL"}


def _signal_is_provably_nonzero(node: ast.expr) -> bool:
    """signal 引数が **0 ではないと静的に断定できる** ときだけ True。

    断定できるのは 2 通りだけ:
      * literal の非 0 (``os.kill(pid, 9)``)
      * ``signal.SIGTERM`` のような本物の signal 名 (値 0 の ``SIG_DFL`` は除く)

    変数・呼び出し・属性参照など「読んだだけでは 0 か分からない」ものは False を
    返す = 検出対象にする (安全側)。0 と分からないものを見逃すと、定数化リファクタ
    で穴が開く。
    """
    if isinstance(node, ast.Constant):
        return isinstance(node.value, int) and node.value != 0
    if isinstance(node, ast.Attribute):
        return node.attr.startswith("SIG") and node.attr not in _ZERO_VALUED_SIGNAL_NAMES
    return False


def _kill_zero_sites(tree: ast.AST, imports_bare_kill: bool) -> list[tuple[int, str]]:
    """Windows-unsafe になりうる ``os.kill`` 呼び出しを ``(行番号, 囲む関数名)`` で返す。

    signal 引数が非 0 と証明できる呼び出し (= 本物の kill) だけを除外し、残りは
    すべて返す。``os.kill(pid, _PROBE)`` / ``os.kill(*args)`` / ``os.kill(pid, sig=0)``
    のような形も拾う (前 2 つは実際に実行可能な Windows-unsafe 経路、3 つ目は
    CPython では TypeError だが検出しても害がないので安全側に含める)。
    """
    # 各ノード → 囲む関数名 を先に引けるようにする。
    enclosing: dict[ast.AST, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                enclosing.setdefault(child, node.name)

    sites: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_os_kill = (
            isinstance(func, ast.Attribute)
            and func.attr == "kill"
            and isinstance(func.value, ast.Name)
            and func.value.id == "os"
        ) or (
            imports_bare_kill and isinstance(func, ast.Name) and func.id == "kill"
        )
        if not is_os_kill:
            continue

        # 引数の形が不透明 (``*args`` / ``**kwargs``) なら signal を読めない → 安全側。
        opaque = any(isinstance(a, ast.Starred) for a in node.args) or any(
            kw.arg is None for kw in node.keywords
        )
        sig: ast.expr | None = None
        if len(node.args) >= 2:
            sig = node.args[1]
        else:
            for kw in node.keywords:
                if kw.arg == "sig":
                    sig = kw.value
                    break

        if not opaque:
            if sig is None:
                # signal 引数が無い = そもそも呼べない (TypeError)。危険ではない。
                continue
            if _signal_is_provably_nonzero(sig):
                continue  # 本物の kill = 正当な操作
        sites.append((node.lineno, enclosing.get(node, "<module>")))
    return sites


def _imports_bare_kill(tree: ast.AST) -> bool:
    """``from os import kill`` があるか (= 素の ``kill(...)`` も検査対象にする)。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "os":
            if any(a.name == "kill" for a in node.names):
                return True
    return False


def _has_posix_gate(tree: ast.AST, probe_name: str) -> list[str]:
    """``probe_name`` を呼ぶ関数のうち、POSIX gate を持たないものの名前を返す。

    gate = 関数本体の文のどこかに ``os.name != "posix"`` を条件とする ``if`` が
    あり、その中で ``return`` すること。コメントではなく構造で確認する。
    """
    ungated: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name == probe_name:
            continue  # probe 自身は呼び出し元ではない
        calls_probe = any(
            isinstance(c, ast.Call)
            and isinstance(c.func, ast.Name)
            and c.func.id == probe_name
            for c in ast.walk(node)
        )
        if not calls_probe:
            continue
        if not _gate_in_body(node):
            ungated.append(node.name)
    return ungated


def _gate_in_body(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """``if os.name != "posix": return`` 相当の早期 return が在るか。"""
    for stmt in ast.walk(func):
        if not isinstance(stmt, ast.If):
            continue
        test = stmt.test
        if not (isinstance(test, ast.Compare) and len(test.ops) == 1):
            continue
        if not isinstance(test.ops[0], ast.NotEq):
            continue
        left, right = test.left, test.comparators[0]
        is_os_name = (
            isinstance(left, ast.Attribute)
            and left.attr == "name"
            and isinstance(left.value, ast.Name)
            and left.value.id == "os"
        )
        is_posix = isinstance(right, ast.Constant) and right.value == "posix"
        if not (is_os_name and is_posix):
            continue
        if any(isinstance(s, ast.Return) for s in ast.walk(stmt)):
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Detect Windows-unsafe os.kill(pid, 0) liveness probes.",
    )
    parser.add_argument(
        "--strict", action="store_true", help="exit 1 on drift (CI gate)"
    )
    parser.add_argument(
        "--root", default=None,
        help="scan this directory instead of the repo root (used by the guard's own test)",
    )
    args = parser.parse_args()

    global REPO_ROOT
    if args.root:
        REPO_ROOT = Path(args.root).resolve()

    problems: list[str] = []

    for path in _iter_python_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        bare = _imports_bare_kill(tree)
        for lineno, func_name in _kill_zero_sites(tree, bare):
            entry = ALLOWLIST.get((rel, func_name))
            if entry is None:
                problems.append(
                    f"{rel}:{lineno}: os.kill in {func_name}() の signal が 0 でないと "
                    f"証明できません — Windows ではこれは生存判定ではなく "
                    f"TerminateProcess になります。"
                    f"`from pid_liveness import pid_alive` を使ってください "
                    f"(lib/pid_liveness.py)。"
                )
                continue
            reason, needs_gate = entry
            if needs_gate:
                ungated = _has_posix_gate(tree, func_name)
                if ungated:
                    problems.append(
                        f"{rel}:{lineno}: {func_name}() は allowlist 上 "
                        f"'呼び出し側が POSIX gate 済み' を根拠に許可されていますが、"
                        f"gate を持たない呼び出し元があります: "
                        f"{', '.join(sorted(ungated))}。gate を戻すか "
                        f"pid_liveness.pid_alive に寄せてください。"
                    )

    if problems:
        sys.stderr.write("[check-pid-liveness] Windows-unsafe な pid 生存判定:\n")
        for p in problems:
            sys.stderr.write(f"  - {p}\n")
        sys.stderr.write(
            "\n背景: ms-133 / e-6591。Python の os.kill は Windows で signal 0 でも\n"
            "TerminateProcess を呼ぶため、生存確認が相手を終了させます。\n"
        )
        return 1 if args.strict else 0

    print(
        "[check-pid-liveness] ok — signal が非 0 と証明できない os.kill は "
        "allowlist 内のみ (literal 0 / 定数 / *args / sig= の各形を検査)。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
