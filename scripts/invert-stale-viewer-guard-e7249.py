#!/usr/bin/env python3
"""反転ハーネス: 建て直し忘れの検出 (e-7249) を壊したら赤くなるかを測る。

**なぜ要るのか**: 2026-10-30 に運用室から端末へ飛ぶ不具合を直して取り込んだのに、
11-01 に同じ症状が観測された。原因はコードではなく、動いていた実行ファイルが 10/28 の
古いもので修正が入っていなかったこと。版はどちらも 0.65.0 だったので版握手 (e-6482) は
通った。つまり **緑のガードが在るのに守られていなかった**。だからこのガードに対しては、
「置いた」ではなく「壊したら赤くなる」まで測る。

**この形を選んだ理由** (`scripts/invert_harness.py` の穴 1〜4 と、PR #818 の独立
レビューの指摘):

* **作業コピーで変異させる**。リポジトリのファイルを in-place で書き換えないので、
  バイトコードキャッシュが変異版を持ち越す穴 (``-B`` では閉じない) と、隣で走る
  pytest が変異中のファイルを読む穴が構造的に消える。
* **入口は共有部品 1 箇所に寄せる** (`ih.guarded_main` = 穴 4)。``--help`` や
  打ち間違えたフラグを黙って捨てて本物の変異を走らせない。引数の裁きと排他ロックを
  自分で書かないので、裁き方が姉妹ハーネスと食い違わない
  (`tests/test_invert_harness_entrypoint_guards.py` が AST で強制している)。
* **赤の理由を構造で見る** (`ih.classify_pytest_result` = 穴 3)。終了コードが非 0
  でも、収集に失敗しただけ (rc 2〜5) や要約行が error を数えている場合は「捕まえた」
  に数えない。変異が構文を壊しただけのケースが「守られている」に化けるのを防ぐ。
* **反転ごとに「赤くなるべきテスト」を名指しする**。
* **目印が見つからなければ失敗扱い**。コードが動いたときに反転を黙って消すと、
  ハーネスが緑のまま中身が空になる (SKIP は緑と同じ害)。

**pytest から呼ぶのはこのハーネスだけ** (= 姉妹の既定と違う点、理由を書き残す):
姉妹は ``lib/`` を in-place で変異させるので pytest に配線できない (CI は ``-n 4``
並列で、隣の worker が変異中のファイルを読む)。だから「手で直列に走らせる」が既定で、
`tests/test_invert_harness_entrypoint_guards.py` もそう明記している。このハーネスは
作業コピーしか変異させないので、その危険が構造的に無く、配線してよい。配線する利点は
**人が思い出さなくても走る**こと —— 手で走らせる約束は、忘れたときに何も言わない。

排他ロックは `guarded_main` が取る。いま pytest から走る locked なハーネスは
これだけなので競合しない。将来もう 1 本を pytest に配線するなら、そこで初めて
``REFUSED: another invert harness is running`` が出るので、黙って壊れることはない。

使い方:
    python3 scripts/invert-stale-viewer-guard-e7249.py
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import invert_harness as ih  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_FILES = (
    "tests/test_cmd_view_stale_binary_e7249.py",
    "tests/test_cmd_view_launcher_ms173.py",
    "tests/test_cmd_view_ms170.py",
)

# (名前, 対象ファイル, 置換前, 置換後, 赤くなるべきテスト)
INVERSIONS = [
    (
        "判定が常に『比べられない』を返す (= 検査の無効化)",
        "lib/cmd_view.py",
        "    newest = newest_source(viewer_dir)\n    if newest is None:",
        "    newest = None\n    if newest is None:",
        ["test_source_newer_than_binary_is_stale",
         "test_binary_newer_than_source_is_fresh",
         "test_version_handshake_passes_on_same_version_staleness"],
    ),
    (
        "比べられないものを fresh に畳む (= 判定できない環境が黙って緑になる)",
        "lib/cmd_view.py",
        'return "unknown", "手元に viewer のソースがありません (配布版)"',
        'return "fresh", "手元に viewer のソースがありません (配布版)"',
        ["test_missing_sources_is_unknown_not_fresh"],
    ),
    (
        "試験ファイルも古さの根拠にする (= 嘘の古さで建て直しを強いる)",
        "lib/cmd_view.py",
        '    if name.endswith("_test.go"):\n        return False\n',
        "",
        ["test_test_sources_are_not_watched",
         "test_newest_source_ignores_test_files"],
    ),
    (
        "古くても委譲を止めない (= 黙って古いまま起動する)",
        "lib/cmd_view.py",
        "        if not skip_staleness:\n"
        "            raise SystemExit(stale_binary_notice(binary, stale_detail))",
        "        if False:\n"
        "            raise SystemExit(stale_binary_notice(binary, stale_detail))",
        ["test_stale_binary_refuses_delegation"],
    ),
    (
        "素通し口では判定そのものを飛ばす (= 古い事実がどこにも残らない)",
        "lib/cmd_view.py",
        "    verdict, stale_detail = binary_freshness(binary)",
        '    verdict, stale_detail = (("fresh", "") if skip_staleness\n'
        "                             else binary_freshness(binary))",
        ["test_skip_env_still_reports_staleness",
         "test_skip_env_does_not_silence_freshness_check"],
    ),
    (
        "案内から『動いている盤を終了する』を落とす (= 一周させる案内)",
        "lib/cmd_view.py",
        '        "    1. 動いている盤をすべて終了する (建て直しより先。実行中のファイルへ"\n'
        '        "上書きすると、出来た実行ファイルが起動できなくなります)\\n"\n',
        '        "    1. いきなり建て直す\\n"\n',
        ["test_notice_names_both_rebuild_and_process_replacement",
         "test_notice_puts_stopping_before_rebuilding"],
    ),
    (
        "案内の順序を逆にする (= 従うと実行ファイルが壊れる案内)",
        "lib/cmd_view.py",
        '        "  dev clone での回復は、この順序で行ってください:\\n"',
        '        "  dev clone での回復は viewer/build.sh の後に:\\n"',
        ["test_notice_puts_stopping_before_rebuilding"],
    ),
    (
        "unknown を stale 扱いにする (= 配布版の利用者を全員止める)",
        "lib/cmd_view.py",
        '    if verdict == "stale":\n',
        '    if verdict in ("stale", "unknown"):\n',
        ["test_unknown_freshness_delegates"],
    ),
    # --- ここから下は独立レビュー (PR#828) 由来の反転 ---
    (
        "診断を stdout に出す (= 委譲先の出力に混ざり、読む側が誤認する)",
        "lib/cmd_view.py",
        '              "BEACON_VIEW_SKIP_STALENESS_CHECK=1 のため起動します",\n'
        "              file=sys.stderr, flush=True)",
        '              "BEACON_VIEW_SKIP_STALENESS_CHECK=1 のため起動します",\n'
        "              file=sys.stdout, flush=True)",
        ["test_staleness_warning_goes_to_stderr_not_stdout"],
    ),
    (
        "ソース走査を浅く戻す (= 入れ子のソースが新しくても見落とす)",
        "lib/cmd_view.py",
        "    for root, dirs, names in walk(viewer_dir):",
        "    for root, dirs, names in list(walk(viewer_dir))[:1]:",
        ["test_newest_source_reaches_into_subdirectories",
         "test_nested_source_newer_than_binary_is_stale"],
    ),
    (
        "testdata を走査対象に戻す (= ビルドに入らない .go を古さの根拠にする)",
        "lib/cmd_view.py",
        "        dirs[:] = [d for d in dirs if d not in VIEWER_SOURCE_SKIP_DIRS]\n",
        "",
        ["test_testdata_go_files_are_not_sources"],
    ),
    (
        "ファイル名の絞り込みを外す (= 実行ファイル自身を古さの根拠にする)",
        "lib/cmd_view.py",
        "            if not _is_viewer_source(name):\n                continue\n",
        "",
        ["test_built_binary_is_not_a_source_by_name",
         "test_testdata_go_files_are_not_sources"],
    ),
    (
        "2 つの素通し口を 1 本に束ね直す (= 名前が予告しない保護まで外れる)",
        "lib/cmd_view.py",
        '    skip_staleness = os.environ.get("BEACON_VIEW_SKIP_STALENESS_CHECK") == "1"',
        "    skip_staleness = skip_handshake",
        ["test_version_handshake_skip_does_not_downgrade_staleness"],
    ),
]


def _scratch_tree() -> str:
    """変異を当てる作業コピーを作る (リポジトリ本体には触らない)。"""
    tmp = tempfile.mkdtemp(prefix="invert-e7249-")
    for sub in ("lib", "scripts", "tests", "bin", "beacon_cli",
                "shared", "skills", "plugins"):
        src = os.path.join(REPO, sub)
        if not os.path.isdir(src):
            continue
        shutil.copytree(src, os.path.join(tmp, sub),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return tmp


def _run(tree: str, tests):
    """作業コピーで pytest を走らせ ``(捕まえたか, 表示用の末尾, 終了コード)``。

    「捕まえたか」の判定は `ih.classify_pytest_result` に委ねる (穴 3)。手元に
    写すと、1 本だけ直って姉妹に旧判定が残る形になる。
    """
    expr = " or ".join(tests)
    r = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", *TEST_FILES, "-q",
         "-p", "no:randomly", "-p", "no:cacheprovider", "-k", f"({expr})"],
        cwd=tree, capture_output=True, text=True, timeout=600,
        env={**ih.isolated_env(), "PYTHONDONTWRITEBYTECODE": "1"},
    )
    tail = (r.stdout + r.stderr).strip().split("\n")[-1]
    caught, shown = ih.classify_pytest_result(r, tail)
    return caught, shown, r


def main() -> int:
    tree = _scratch_tree()
    failures = []
    try:
        all_tests = sorted({t for inv in INVERSIONS for t in inv[4]})
        base_caught, base_tail, base_proc = _run(tree, all_tests)
        if base_proc.returncode != 0:
            print("基準が緑ではありません。反転の前にテストを直してください:")
            print(base_tail)
            print((base_proc.stdout + base_proc.stderr)[-4000:])
            return 1
        print(f"基準は緑 ({len(all_tests)} 件): {base_tail}\n")

        for name, rel, old, new, tests in INVERSIONS:
            path = os.path.join(tree, rel)
            with open(path, encoding="utf-8") as f:
                original = f.read()
            if original.count(old) != 1:
                failures.append(
                    f"{name}: 目印が {rel} に 1 箇所見つかりません "
                    f"({original.count(old)} 件)。コードが動いたので反転を"
                    "更新してください (消してはいけません)。")
                print(f"  目印なし   {name}")
                continue
            with open(path, "w", encoding="utf-8") as f:
                f.write(original.replace(old, new, 1))
            try:
                caught, shown, _proc = _run(tree, tests)
                if caught:
                    print(f"  赤くなった {name}")
                else:
                    # 「赤くならなかった」と「赤の理由が違った (収集失敗等)」を
                    # 区別して報告する。後者を『守られている』に数えないのが
                    # ih.classify_pytest_result の役目 (穴 3)。
                    failures.append(
                        f"{name}: {rel} の機構を外しても "
                        f"{', '.join(tests)} が守りませんでした — {shown}")
                    print(f"  守れず     {name} — {shown}")
            finally:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(original)
    finally:
        shutil.rmtree(tree, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} 件の反転が赤を作れませんでした:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"OK: {len(INVERSIONS)} 件すべてが名指しのテストを赤にしました。")
    return 0


if __name__ == "__main__":
    # 入口は共有部品 1 箇所に寄せる (引数の裁き + 排他ロック)。
    # `tests/test_invert_harness_entrypoint_guards.py` がこれを AST で強制する。
    sys.exit(ih.guarded_main(main, sys.argv, __doc__, pathlib.Path(REPO)))
