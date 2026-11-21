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
SPINE = ROOT / "lib" / "review_spine.py"
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
    ("共通の仕上げ口から落とし処理を外す (最後の関門)",
     "    artifact, dropped = strip_artifact_bookkeeping(artifact)\n",
     "    dropped = []\n",
     True),
    ("兄弟の組み立て口 (目的達成) から仕上げ口を外す",
     "    _attainment_artifact, gaps = judge_artifact(\n",
     "    _attainment_artifact, gaps = (\n",
     True),
    ("引用符つき path の復号を外す (非 ASCII 名の作業記録が漏れる)",
     "            ref = _strip_ab_prefix(unquote_git_path(line[4:].strip()))\n",
     "            ref = _strip_ab_prefix(line[4:].strip())\n",
     True),
    # ms-119 e-7520: ヘッダー無しセクションの path 割り出しを、修正前の
    # 「最初の " b/" で割る」形に戻す。名前に " b/" を含むファイルから作業記録へ
    # 移した差分が落ちなくなるので、赤くならなければ検査がその分岐を見ていない。
    ("ヘッダー無しセクションの割り出しを最短一致に戻す",
     "    if not paths and section_lines:\n"
     "        sides = _side_line_paths(section_lines)\n"
     "        if len(sides) >= 2:\n"
     "            return sides\n"
     '        paths.extend(_diff_git_line_paths(section_lines[0].rstrip("\\n")))\n',
     "    if not paths and section_lines:\n"
     '        m = _re.match(r"^diff --git a/(.+?) b/(.+)$",\n'
     '                      section_lines[0].rstrip("\\n"))\n'
     "        if m:\n"
     "            paths.extend([m.group(1), m.group(2)])\n",
     True),
    # 最初はこれを「緑のままが正しい」と予想して外した。落とす/落とさないの判断は
    # 候補を全部返す側が受け止めるので、確かに漏れはしない。だが path の **名指し**
    # が候補 4 つに崩れ、落としたことの開示が嘘の path を名乗る。検査はそこで赤に
    # なった —— 予想ではなく実測が正しい。
    ("rename の行を読まない (diff --git 行だけで割る)",
     "        sides = _side_line_paths(section_lines)\n"
     "        if len(sides) >= 2:\n"
     "            return sides\n",
     "        sides = []\n"
     "        if len(sides) >= 2:\n"
     "            return sides\n",
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
    ("新しい収集経路: subprocess.check_output で採る", '''

def _probe_check_output(ref):
    return subprocess.check_output(["git", "diff", "-U25", ref], text=True)
''', True),
    ("新しい収集経路: 空リストから append で argv を組む", '''

def _probe_append_argv(ref):
    argv = []
    argv.append("git")
    argv.append("diff")
    argv.append(ref)
    return subprocess.run(argv, capture_output=True, text=True).stdout
''', True),
    ("新しい収集経路: git diff-tree -p で採る", '''

def _probe_diff_tree(ref):
    return subprocess.run(["git", "diff-tree", "-p", ref],
                          capture_output=True, text=True).stdout
''', True),
    ("新しい収集経路: os.system で採る", '''

def _probe_os_system(ref):
    import os as _os
    return _os.system("git diff -U25 " + ref)
''', True),
    # ms-119 e-7519: 呼び出し先を属性名だけで判定していた時期は、下の 2 つが
    # どちらも誤っていた —— 別名 import は見落とし (緑のまま)、無関係な .run()
    # は偽の赤。束縛元を import 文から解決して両方を閉じた。
    ("新しい収集経路: 別名 import した run で採る", '''

from subprocess import run as _aliased_run


def _probe_aliased_run(ref):
    return _aliased_run(["git", "diff", "-U25", ref],
                        capture_output=True, text=True).stdout
''', True),
    ("新しい収集経路: 関数の中で別名 import した check_output で採る", '''

def _probe_aliased_check_output(ref):
    from subprocess import check_output as _co
    return _co(["git", "diff", "-U25", ref], text=True)
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
    ("git diff だが統計のみ (本文を出さない)", '''

def _probe_diff_stat(ref):
    return subprocess.run(["git", "diff", "--stat", ref],
                          capture_output=True, text=True).stdout
''', False),
    ("非差分コマンドに後から引数を足す", '''

def _probe_append_safe(pr):
    argv = ["gh", "pr", "view", str(pr), "--json", "title"]
    argv += ["--repo", "o/r"]
    return subprocess.run(argv, capture_output=True, text=True).stdout
''', False),
    ("gh の引数の値が diff という文字列 (差分ではない)", '''

def _probe_gh_label_diff():
    return subprocess.run(["gh", "issue", "list", "--label", "diff"],
                          capture_output=True, text=True).stdout
''', False),
    ("gh だが差分ではない (変数経由)", '''

def _probe_gh_view(pr):
    argv = ["gh", "pr", "view", str(pr), "--json", "title"]
    return subprocess.run(argv, capture_output=True, text=True).stdout
''', False),
    # ms-119 e-7519 の偽の赤。``run`` はありふれたメソッド名なので、差分と無関係な
    # オブジェクトの ``.run()`` で赤くなると、踏んだ人は述語を緩める方向に直す。
    ("無関係なオブジェクトの .run() を呼ぶ (差分とは無関係)", '''

class _ProbePipeline:
    def run(self, payload):
        return payload


def _probe_unrelated_run(pipeline, payload):
    return pipeline.run(payload)
''', False),
    ("実行しない名前を subprocess から import する", '''

def _probe_pipe_constant(cmd):
    from subprocess import PIPE
    return (cmd, PIPE)
''', False),
]

# (名前, 差分テキスト, 落ちるべきか) — 採る側を通さず、落とす述語を直接測る。
# ms-119 e-7520: ヘッダーを持たないセクション (100% 一致の rename / mode のみ) は
# ``diff --git a/X b/Y`` の 1 行から 2 つの path を割り出すしかない。以前は最初の
# ``" b/"`` で割っており、X 自身がそれを含むと割り位置を誤った。下の TRICKY は
# ``weird b/`` というディレクトリを作って git mv した実出力と同じ形。
_CODE_SECTION = ("diff --git a/engine/run.py b/engine/run.py\n"
                 "--- a/engine/run.py\n+++ b/engine/run.py\n@@ -1 +1 @@\n-x\n+y\n")

STRIP_CASES = [
    ("ヘッダー無し rename: 紛らわしい名前から作業記録へ",
     "diff --git a/weird b/case.txt b/.beacon/thing.txt\n"
     "similarity index 100%\n"
     "rename from weird b/case.txt\n"
     "rename to .beacon/thing.txt\n", True),
    ("ヘッダー無し rename: 普通の名前から作業記録へ",
     "diff --git a/old.txt b/.beacon/moved.txt\n"
     "similarity index 100%\n"
     "rename from old.txt\n"
     "rename to .beacon/moved.txt\n", True),
    ("ヘッダー無し rename: 作業記録から外へ (片側一致でも落ちる)",
     "diff --git a/.beacon/thing.txt b/weird b/case.txt\n"
     "similarity index 100%\n"
     "rename from .beacon/thing.txt\n"
     "rename to weird b/case.txt\n", True),
    ("ヘッダー無し mode 変更: 紛らわしい名前 (作業記録ではない)",
     "diff --git a/weird b/case.txt b/weird b/case.txt\n"
     "old mode 100644\nnew mode 100755\n", False),
    ("ヘッダー無し rename: 作業記録と無関係 (偽の drop を出さない)",
     "diff --git a/weird b/case.txt b/other b/place.txt\n"
     "similarity index 100%\n"
     "rename from weird b/case.txt\n"
     "rename to other b/place.txt\n", False),
]


def check_strip_cases():
    """落とす述語を直接測る。(不合格の説明, ...) を返す。"""
    sys.path.insert(0, str(ROOT / "lib"))
    import review_spine as rs
    bad = []
    print("\n--- 落とす述語を直接測る (ヘッダー無しセクション) ---")
    for name, section, should_drop in STRIP_CASES:
        out, dropped = rs.strip_bookkeeping_from_diff(_CODE_SECTION + section)
        got = bool(dropped)
        ok = got == should_drop and "engine/run.py" in out
        print(f"  [{'OK' if ok else 'NG'}] {name}: "
              f"期待={'落とす' if should_drop else '残す'} "
              f"実測={'落とす' if got else '残す'} ({dropped})")
        if not ok:
            bad.append(f"{name}: 期待={'落とす' if should_drop else '残す'} "
                       f"実測={'落とす' if got else '残す'}")
    return bad


def run_tests():
    """(rc, 末尾の要約行) — パイプを挟まずに rc を測る。"""
    p = subprocess.run([sys.executable, "-B", "-m", "pytest", *TESTS, "-q"],
                       cwd=ROOT, capture_output=True, text=True)
    tail = [L for L in (p.stdout or "").splitlines()
            if "passed" in L or "failed" in L or "error" in L]
    return p.returncode, (tail[-1] if tail else "(要約行なし)")


def main():
    original = TARGET.read_text(encoding="utf-8")
    spine_original = SPINE.read_text(encoding="utf-8")
    rc, summary = run_tests()
    if rc != 0:
        print(f"BASELINE が赤い: {summary}\n  変異を測る前に直すこと。")
        return 1
    print(f"baseline: 緑 ({summary})\n")

    bad = []
    try:
        print("--- 変異 (既存実装を壊す) ---")
        for name, before, after, should_fail in MUTATIONS:
            # 毎周の頭で両方を戻す。片方だけ戻すと、前の周の変異が残ったまま
            # 次を測ることになり、「緑であるべき」が全部赤になる (実際に踏んだ)。
            TARGET.write_text(original, encoding="utf-8")
            SPINE.write_text(spine_original, encoding="utf-8")
            path, base = ((SPINE, spine_original) if before in spine_original
                          else (TARGET, original))
            if base.count(before) != 1:
                bad.append(f"{name}: 置換前の文字列が {base.count(before)} 回 — "
                           f"harness が実装に追随していない (測れていない)")
                print(f"  [NG] {name}: 置換対象が見つからない")
                continue
            path.write_text(base.replace(before, after), encoding="utf-8")
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
            SPINE.write_text(spine_original, encoding="utf-8")
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
        SPINE.write_text(spine_original, encoding="utf-8")

    rc, summary = run_tests()
    if rc != 0:
        bad.append(f"復元後も赤い: {summary}")
    else:
        print(f"\n復元後: 緑 ({summary})")

    # 復元後の実装で測る (変異が残っていると結果が濁るので、必ずこの位置)。
    bad.extend(check_strip_cases())

    if bad:
        print("\n不合格:")
        for b in bad:
            print(f"  - {b}")
        return 1
    print("\n全件合格 — 契約違反は赤、契約を保った追記は緑のまま")
    return 0


if __name__ == "__main__":
    sys.exit(main())
