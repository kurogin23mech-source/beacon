#!/usr/bin/env python3
"""両フロントが同じ動詞を同じ内部処理へ送っていることを検査する (ms-181 e-7102).

塞いでいる穴
============
CLI には入口が 2 つあり (bash の ``bin/beacon`` と Python の
``beacon_cli/dispatch.py``)、どちらも ``commands.py`` に渡す **内部の処理名を
文字列で**指定している。``_run_commands_py(root, "recap_targets", …)`` と
``python3 "$COMMANDS_PY" recap_targets`` のように。

片方がこの文字列を**別の動詞のものに取り違えても**、既存のガードはすべて
素通りする。ms-184 fork が実測し、2026-10-20 に本スクリプトの導入前後で
再実測した (下の「既存ガードが見られない理由」を参照)。取り違えた側の端末では
``beacon recap targets`` が黙って ``recap projects`` の答えを返す — 終了コードは
0 で、出力も JSON として妥当なので、呼び出し側には区別がつかない。

``recap`` だけは両フロントを同じ引数で実起動して env を比べる専用試験
(``tests/test_recap_frontend_parity_ms184.py``) で閉じたが、それは **1 動詞族を
名指しで守る形**で、後から足した動詞は入らない。このスクリプトは動詞を
**help registry から列挙**するので、registry に載った動詞は自動で検査対象になる。

既存ガードが見られない理由 (受入条件 4、2026-10-20 実測)
========================================================
``scripts/check-cli-help-drift.py`` は **旗** の集合を比べる。処理名は旗では
ないので見ていない。``scripts/check-verb-ledger.py`` は動詞が台帳に載っているかを
見るが、動詞がどの処理へ行くかは見ない。``scripts/check-capability-scope.py`` は
capability の共有スコープ (L0〜L4) を見るので、やはり対応先は見ない。
3 つとも「動詞が在る / 旗が在る / 層が正しい」を見ており、**「その動詞がどこへ
行くか」を見るものが 1 つも無かった**。本スクリプトがその 1 つ。

どう測るか — 静的に読むのを **やめた** 理由
==========================================
最初はソースから静的に抽出した。``check-cli-help-drift`` の動詞パス → bash 領域の
解決を再利用して、辿り着いた領域の ``$COMMANDS_PY <処理名>`` を拾う形。
**これは偽の緑だった** (2026-10-20、導入前に反転で実測):

``beacon recap`` の sub 動詞の振り分けは ``case`` の arm ではなく
``if [ "$recap_sub" = "targets" ]`` という **条件分岐**で書かれている。領域の
解決は arm 単位なので、``recap targets`` を引いても ``recap)`` の arm 全体が返り、
``recap_targets`` と ``recap_projects`` の **両方**が混ざって見える。だから
python 側を ``recap_projects`` に取り違えさせても集合が交差してしまい、
**検査は通ってしまった**。領域を arm で切る方法では、``if`` で振り分ける動詞を
原理的に分離できない。

そこで **両フロントを実際に起動して、渡される処理名を観測する**。ms-184 が
``recap`` 1 動詞に対して採った方法を、registry の全動詞に広げた形。

**両フロントとも影の木の subprocess として起動する**。python 側を同一プロセスで
``dispatch()`` する案を先に書いたが捨てた: ハンドラは ``_run_commands_py`` に
辿り着く前に実際の処理をするものがあり (``_handle_init`` は ``.beacon/cloud.json``
を書く / ``_relocate_to_project_root`` は ``os.chdir`` する)、**本物のリポジトリに
対して 216 動詞を走らせることになる**。観測のために観測対象を汚すのは筋が悪い。
subprocess に揃えると、封じ込めが両フロントで同一になり、片方だけ安全という
非対称も消える。

封じ込めは 2 枚:
  1. ``lib/commands.py`` を **処理名を印字するだけのスタブ**に差し替える
     (= 本物の処理は一切走らない)。
  2. ``PATH`` の先頭に **外部コマンドの無効化 shim** を置く (``git`` / ``gh`` /
     ``curl`` / ``ssh`` / ``node`` / ``npm`` / ``tmux`` …)。dispatch の前に
     外部コマンドを叩く arm が在っても、何も起きない。実データにも、ネットワークにも
     触らない。

位置引数が要る動詞は両フロントとも dispatch せずに終わる (= 使い方を出して終了)。
その場合は「両方 dispatch しない」ので比較対象から外れる — 片方だけが dispatch
したならそれ自体が非対称なので報告する。

判定は **文字列の部分一致ではなく集合の交差** (受入条件 1)。両フロントが
dispatch する動詞パスについて、処理名の集合が交差しなければ取り違え。

ベースライン (2026-10-20): registry 動詞パス 216 件のうち、両フロントが dispatch
するのは 85 件、不一致は **0 件**。許可リストを持たない厳格な検査にできた
(既存の不一致を抱えた検査は、新しい不一致を数の中に埋めてしまう)。

片側しか dispatch しない動詞 (bash のみ 126 / python のみ 5) は、この検査の
対象ではない。Python フロントが実装していない動詞が在るのは既知の非対称で、
``check-cli-help-drift`` の allowlist が所管している。ここで二重に数えない。
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

def _load_drift_module():
    """Borrow the help-drift checker's registry reader.

    Only ``_registry_entries`` / ``_registry_path`` are used from it — the verb
    LIST. Its bash-region resolution is deliberately not reused any more: that
    is what produced the false green described above.
    """
    path = REPO / "scripts" / "check-cli-help-drift.py"
    spec = importlib.util.spec_from_file_location("_cli_help_drift", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


#: Printed by the stub ``commands.py`` so the probe can read back the handler
#: name the bash front chose. Prefixed so it cannot be confused with anything
#: a bash arm echoes on its own.
_MARK = "__HANDLER__:"

#: External commands neutralised while probing. A bash arm may touch any of
#: these BEFORE it dispatches (``milestone start`` branches with git, the cloud
#: verbs reach the network, ``view`` opens a browser). Shimming them is what
#: makes running every verb safe; ``python3`` is deliberately NOT shimmed
#: because the stub commands.py is how the handler name is observed.
_SHIMMED = ("git", "gh", "curl", "wget", "ssh", "scp", "rsync", "node", "npm",
            "npx", "tmux", "open", "osascript", "docker", "systemctl", "pip",
            "pip3", "brew", "pbcopy", "say")

_STUB_COMMANDS_PY = (
    "import sys\n"
    "print('" + _MARK + "' + (sys.argv[1] if len(sys.argv) > 1 else ''))\n"
)


def _build_probe_tree(tmpdir: Path) -> Path:
    """A shadow tree where running any verb is inert.

    ``bin/`` is copied so the real dispatcher logic is what gets exercised;
    ``lib/commands.py`` is a stub that only prints the handler name it was
    handed; a ``.beacon/project.json`` probe keeps ``ensure_project`` happy so
    the probe reaches dispatch instead of stopping at "not a project".
    """
    import shutil
    root = tmpdir / "tree"
    shutil.copytree(REPO / "bin", root / "bin")
    # The Python front is probed in this tree too, so its package has to live
    # here: resolving the repo root from the real checkout would make the probe
    # dispatch against real data.
    shutil.copytree(REPO / "beacon_cli", root / "beacon_cli",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (root / "lib").mkdir(parents=True, exist_ok=True)
    (root / "lib" / "commands.py").write_text(_STUB_COMMANDS_PY,
                                              encoding="utf-8")
    (root / ".beacon").mkdir(exist_ok=True)
    (root / ".beacon" / "project.json").write_text(
        json.dumps({"name": "handler-parity-probe", "profession": "dev",
                    "milestones": []}), encoding="utf-8")

    shim = tmpdir / "shim"
    shim.mkdir()
    for name in _SHIMMED:
        target = shim / name
        target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        target.chmod(0o755)
    return root


def bash_handlers_for_path(tokens, tree: Path, shim: Path) -> set:
    """Handler names the bash front actually passes, observed by running it.

    Static extraction was tried first and was a false green — see the module
    docstring. Containment is the stub ``commands.py`` plus the PATH shim, so
    this touches no real data and makes no network call.
    """
    import subprocess
    env = dict(os.environ)
    env.pop("BEACON_PROJECT_FILE", None)
    env["PATH"] = str(shim) + os.pathsep + env.get("PATH", "")
    env["BEACON_SESSION_KIND"] = "human"
    try:
        proc = subprocess.run(
            ["bash", str(tree / "bin" / "beacon"), *tokens],
            capture_output=True, text=True, cwd=str(tree), env=env,
            # stdin closed on purpose: several verbs prompt when they get a
            # TTY, and an inherited stdin turns the probe into a hang rather
            # than an observation. Closed stdin reads as EOF, which is what a
            # non-interactive caller sees anyway.
            stdin=subprocess.DEVNULL, timeout=20)
    except subprocess.TimeoutExpired:
        # Recorded as "did not dispatch" rather than crashing the run: a verb
        # that blocks with no stdin is its own problem, not a parity finding.
        return set()
    out = proc.stdout + proc.stderr
    return {line.split(_MARK, 1)[1].strip()
            for line in out.splitlines() if _MARK in line
            and line.split(_MARK, 1)[1].strip()}


def _probe(argv, tree: Path, shim: Path, extra_env=None) -> set:
    """Run one front in the probe tree and read back the handler names.

    Containment: the stub ``commands.py`` (nothing real executes) plus the PATH
    shim (no git / network / browser). ``stdin`` is closed because several verbs
    prompt when they get a TTY, and an inherited stdin turns the probe into a
    hang instead of an observation.
    """
    import subprocess
    env = dict(os.environ)
    env.pop("BEACON_PROJECT_FILE", None)
    env["PATH"] = str(shim) + os.pathsep + env.get("PATH", "")
    env["BEACON_SESSION_KIND"] = "human"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra_env or {})
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, cwd=str(tree), env=env,
            stdin=subprocess.DEVNULL, timeout=20)
    except subprocess.TimeoutExpired:
        # "did not dispatch" rather than crashing the run: a verb that blocks
        # with no stdin is its own problem, not a parity finding.
        return set()
    out = proc.stdout + proc.stderr
    return {line.split(_MARK, 1)[1].strip()
            for line in out.splitlines() if _MARK in line
            and line.split(_MARK, 1)[1].strip()}


def bash_handlers_for_path(tokens, tree: Path, shim: Path) -> set:
    """Handler names the bash front actually passes, observed by running it.

    Static extraction was tried first and was a false green — see the module
    docstring.
    """
    return _probe(["bash", str(tree / "bin" / "beacon"), *tokens], tree, shim)


def python_handlers_for_path(tokens, tree: Path, shim: Path) -> set:
    """Handler names the Python front passes, observed by running it.

    ``BEACON_NO_BASH=1`` is what makes this the Python path: on macOS the entry
    point delegates to ``bin/beacon`` unless told not to, so without it this
    would measure bash twice and report perfect agreement with itself.
    """
    return _probe([sys.executable, "-B", "-m", "beacon_cli.main", *tokens],
                  tree, shim,
                  {"BEACON_NO_BASH": "1", "PYTHONPATH": str(tree)})


def collect(drift=None, only=None) -> dict:
    """Compare both fronts for every verb path the help registry declares.

    ``only`` restricts the probe to the given verb paths (each a string like
    ``"recap targets"``). The full sweep is ~45s, which is right for a CI gate
    and wrong for a unit test, so the test that proves this guard actually
    fires narrows to one verb and the CI script takes the breadth.
    """
    import tempfile
    import shutil as _shutil
    drift = drift or _load_drift_module()
    paths = []
    for entry in drift._registry_entries(drift.COMMANDS_PY):
        tokens = drift._registry_path(entry.get("command", ""))
        if tokens:
            paths.append(tuple(tokens))
    paths = sorted(set(paths))
    if only:
        wanted = {tuple(v.split()) for v in only}
        missing = wanted - set(paths)
        if missing:
            raise SystemExit(
                "--verb named paths the help registry does not declare: "
                + ", ".join(" ".join(m) for m in sorted(missing)))
        paths = [p for p in paths if p in wanted]

    mismatches = []
    compared = []
    bash_only = []
    python_only = []
    tmpdir = Path(tempfile.mkdtemp(prefix="handler-parity-"))
    try:
        tree = _build_probe_tree(tmpdir)
        shim = tmpdir / "shim"
        for path in paths:
            b = bash_handlers_for_path(list(path), tree, shim)
            y = python_handlers_for_path(list(path), tree, shim)
            if not b and not y:
                continue
            if not y:
                bash_only.append(" ".join(path))
                continue
            if not b:
                python_only.append(" ".join(path))
                continue
            compared.append(" ".join(path))
            if not (b & y):
                mismatches.append({
                    "verb": " ".join(path),
                    "bash": sorted(b),
                    "python": sorted(y),
                })
    finally:
        _shutil.rmtree(tmpdir, ignore_errors=True)
    return {
        "ok": not mismatches,
        "verb_paths": len(paths),
        "compared": compared,
        "mismatches": mismatches,
        "bash_only": bash_only,
        "python_only": python_only,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true",
                    help="accepted for symmetry with the sibling guards; this "
                         "check has no lenient mode (its baseline is 0 "
                         "mismatches, so there is nothing to grandfather)")
    ap.add_argument("--verb", action="append", default=None,
                    help='probe only this verb path, e.g. --verb "recap targets" '
                         "(repeatable). Omit for the full sweep.")
    args = ap.parse_args()

    result = collect(only=args.verb)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ok"] else 1

    if result["ok"]:
        print(f"[handler-parity] OK: 動詞パス {result['verb_paths']} 件のうち "
              f"両フロントが dispatch する {len(result['compared'])} 件で、"
              "内部処理名が一致しています "
              f"(bash のみ {len(result['bash_only'])} / "
              f"python のみ {len(result['python_only'])} 件は対象外)。")
        return 0

    print("[handler-parity] 両フロントが同じ動詞を違う内部処理へ送っています:",
          file=sys.stderr)
    for row in result["mismatches"]:
        print(f"  - beacon {row['verb']}", file=sys.stderr)
        print(f"      bash   → {row['bash']}", file=sys.stderr)
        print(f"      python → {row['python']}", file=sys.stderr)
    print("  片方の端末だけが別の動詞の答えを返します (終了コードは 0 なので "
          "呼び出し側には区別がつきません)。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
