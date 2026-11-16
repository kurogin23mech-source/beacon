#!/usr/bin/env python3
"""ms-119 e-7352: 差分から作業記録を落とすガードを、壊して赤を確かめる。

**なぜ probe が要るか** (独立 AX レビュー 2026-11-16): 初版のこのハーネスは
既存関数を変異させるだけで、「新しい収集経路を足す」形を一度も試していなかった。
その結果、当時のガードが 4 つの形 (変数経由の argv / `git log -p` / 別モジュール /
strip の名前を docstring に書いただけ) を緑のまま通していたのを捕まえられなかった。
judge が実際にその 4 形を書いて実測し、穴として報告した。

そこでこのハーネスは 2 種類を回す:

* **変異**: 既存の実装を壊す (契約違反 → 赤、契約維持 → 緑のまま)
* **probe**: 新しい収集経路を **足す** (落とさない形 → 赤、落とさない形でない → 緑)

使い方: python3 -B scripts/invert-whole-target-bookkeeping-guard.py
(``-B`` は必須。stale な .pyc が偽の緑 / 赤を作る。)
"""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TARGET = ROOT / "lib" / "commands.py"
TESTS = [
    "tests/test_review_bundle_bookkeeping_e7352.py",
    "tests/test_review_whole_target_artifact.py",
]

# (名前, 置換前, 置換後, 赤くなるべきか)
MUTATIONS = [
    ("唯一の口から落とす処理を外す",
     '    diff_text, dropped = _rs.strip_bookkeeping_from_diff(proc.stdout or "")\n',
     '    diff_text, dropped = (proc.stdout or ""), []\n',
     True),
    ("落としたことの開示を外す (target 全体)",
     "    bookkeeping_gap = _rs.bookkeeping_drop_gap(dropped_all)\n",
     '    bookkeeping_gap = ""\n',
     True),
    ("作業記録だけの commit の理由を『変更なし』に潰す",
     '                "bookkeeping-only (この commit は Beacon の作業記録しか変えていない)"\n',
     '                "no-textual-diff (空 / メタのみの commit の可能性)"\n',
     True),
    ("中身を変えずに局所変数名だけ変える (契約は保たれる)",
     '    diff_text, dropped = _rs.strip_bookkeeping_from_diff(proc.stdout or "")\n'
     "    return diff_text, dropped, \"\"\n",
     '    _t, _d = _rs.strip_bookkeeping_from_diff(proc.stdout or "")\n'
     '    return _t, _d, ""\n',
     False),
]

# (名前, 追記するコード, 赤くなるべきか)
PROBES = [
    ("新しい収集経路: argv をリテラルで直書き", '''

def _probe_literal(ref):
    return subprocess.run(["git", "diff", "-U25", ref],
                          capture_output=True, text=True).stdout
''', True),
    ("新しい収集経路: argv を変数に束縛してから渡す", '''

def _probe_var_argv(ref):
    argv = ["git", "diff", "-U25", ref]
    return subprocess.run(argv, capture_output=True, text=True).stdout
''', True),
    ("新しい収集経路: git log -p で採る", '''

def _probe_log_patch(ref):
    return subprocess.run(["git", "log", "-p", "-U25", ref],
                          capture_output=True, text=True).stdout
''', True),
    ("新しい収集経路: 落とす処理の名前を docstring に書いただけ", '''

def _probe_docstring_only(ref):
    """strip_bookkeeping_from_diff を通している (と書いてあるだけで呼んでいない)。"""
    return subprocess.run(["git", "diff", "-U25", ref],
                          capture_output=True, text=True).stdout
''', True),
    ("新しい収集経路: 汎用ランナー経由で採る", '''

def _probe_via_generic_runner(ref):
    return _git_ok(["git", "diff", "-U25", ref])[1]
''', True),
    ("新しい収集経路: 分岐で argv を差し替え、片方だけ差分", '''

def _probe_branching_argv(ref, want_diff):
    argv = ["git", "rev-parse", ref]
    if want_diff:
        argv = ["git", "diff", "-U25", ref]
    return subprocess.run(argv, capture_output=True, text=True).stdout
''', True),
    ("落とす処理を 2 箇所目に増やす", '''

def _probe_second_strip(text):
    import review_spine as _rs2
    return _rs2.strip_bookkeeping_from_diff(text)[0]
''', True),
    # --- 契約を破っていない追記。緑のままでなければ検査が形を測っている ---
    ("差分を産まない git (引数は変数)", '''

def _probe_non_diff_var(sha):
    return subprocess.run(["git", "cat-file", "-e", sha],
                          capture_output=True, text=True).returncode
''', False),
    ("git show だが名前だけ (差分ではない)", '''

def _probe_show_names_only(sha):
    return subprocess.run(["git", "show", "--name-only", "--format=", sha],
                          capture_output=True, text=True).stdout
''', False),
    ("gh だが差分ではない (変数経由)", '''

def _probe_gh_view(pr):
    argv = ["gh", "pr", "view", str(pr), "--json", "title"]
    return subprocess.run(argv, capture_output=True, text=True).stdout
''', False),
]


def run_tests():
    """(rc, 末尾の要約行) — パイプを挟まずに rc を測る。"""
    p = subprocess.run([sys.executable, "-B", "-m", "pytest", *TESTS, "-q"],
                       cwd=ROOT, capture_output=True, text=True)
    tail = [L for L in (p.stdout or "").splitlines()
            if "passed" in L or "failed" in L or "error" in L]
    return p.returncode, (tail[-1] if tail else "(要約行なし)")


def main():
    original = TARGET.read_text(encoding="utf-8")
    rc, summary = run_tests()
    if rc != 0:
        print(f"BASELINE が赤い: {summary}\n  変異を測る前に直すこと。")
        return 1
    print(f"baseline: 緑 ({summary})\n")

    bad = []
    try:
        print("--- 変異 (既存実装を壊す) ---")
        for name, before, after, should_fail in MUTATIONS:
            if original.count(before) != 1:
                bad.append(f"{name}: 置換前の文字列が {original.count(before)} 回 — "
                           f"harness が実装に追随していない (測れていない)")
                print(f"  [NG] {name}: 置換対象が見つからない")
                continue
            TARGET.write_text(original.replace(before, after), encoding="utf-8")
            rc, summary = run_tests()
            got_red = rc != 0
            ok = got_red == should_fail
            print(f"  [{'OK' if ok else 'NG'}] {name}: "
                  f"期待={'赤' if should_fail else '緑'} "
                  f"実測={'赤' if got_red else '緑'}")
            if not ok:
                bad.append(f"{name}: 期待={'赤' if should_fail else '緑'} "
                           f"実測={'赤' if got_red else '緑'}")

        print("\n--- probe (新しい収集経路を足す) ---")
        for name, code, should_fail in PROBES:
            TARGET.write_text(original + code, encoding="utf-8")
            rc, summary = run_tests()
            got_red = rc != 0
            ok = got_red == should_fail
            print(f"  [{'OK' if ok else 'NG'}] {name}: "
                  f"期待={'赤' if should_fail else '緑'} "
                  f"実測={'赤' if got_red else '緑'}")
            if not ok:
                bad.append(f"{name}: 期待={'赤' if should_fail else '緑'} "
                           f"実測={'赤' if got_red else '緑'}")
    finally:
        TARGET.write_text(original, encoding="utf-8")

    rc, summary = run_tests()
    if rc != 0:
        bad.append(f"復元後も赤い: {summary}")
    else:
        print(f"\n復元後: 緑 ({summary})")

    if bad:
        print("\n不合格:")
        for b in bad:
            print(f"  - {b}")
        return 1
    print("\n全件合格 — 契約違反は赤、契約を保った追記は緑のまま")
    return 0


if __name__ == "__main__":
    sys.exit(main())
