"""worktree 判定の人口調査ガードが、増減の**両方**で赤くなることを測る (e-6932)。

このガードは「宣言ではなく機械で閉じる」ための道具なので、**それ自身が壊れたときに
赤くなる**ことを測らないと意味がない。緑のガードは読み手に信頼されるので、偽の安全は
ガード無しより悪い (CORE doc `jnFsoNaNvuZegI73noCn`)。

免除リスト型のガードは「増えたら赤」だけを測りがちだが、これは **人口調査 (census)**
なので減っても赤くなる必要がある (集合から消えた = 宣言が古くなった、も検知対象)。
だから両方向を測る。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "check-worktree-probe-census.py"


def _run_guard(cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(GUARD)],
                          capture_output=True, text=True, cwd=str(cwd))


def test_the_guard_is_green_on_the_real_tree():
    """実ツリーで台帳と実態が一致していること。"""
    r = _run_guard(ROOT)
    assert r.returncode == 0, (
        "実ツリーで台帳とズレている:\n" + r.stdout + r.stderr)
    assert "台帳どおり" in r.stdout, r.stdout


def test_the_guard_runs_without_pytest():
    """pytest に依存しない素のスクリプトであること。

    `ci-strict-drift-guards.sh` は pytest が居ない lint-docs からも呼ばれる。
    import 時に pytest を要求すると、手元は緑で CI だけ赤くなる。
    """
    src = GUARD.read_text(encoding="utf-8")
    assert "import pytest" not in src, "pytest に依存している"
    assert "\nimport ast" in src, "構文木で検査していない (緩い一致は偽の安全)"


def test_a_new_site_turns_it_red(tmp_path):
    """**増えたら赤**: 台帳に無い場所で同じ問いを立てたら検知すること。"""
    probe = ROOT / "lib" / "_census_probe_new_site.py"
    probe.write_text(
        "import subprocess\n"
        "def _sneaky_worktree_probe():\n"
        "    return subprocess.run(['git', 'rev-parse', '--git-common-dir'],\n"
        "                          capture_output=True, text=True)\n",
        encoding="utf-8")
    try:
        r = _run_guard(ROOT)
    finally:
        probe.rename(ROOT / ".trash" / "_census_probe_new_site.py.used")
    assert r.returncode != 0, (
        "台帳に無い新しい箇所が検知されなかった:\n" + r.stdout + r.stderr)
    assert "_sneaky_worktree_probe" in (r.stdout + r.stderr), r.stdout + r.stderr
    assert "台帳に無い" in (r.stdout + r.stderr), r.stdout + r.stderr


def test_a_removed_site_turns_it_red(tmp_path):
    """**減っても赤**: 台帳にあるのに実態から消えたら検知すること。

    免除リストなら「減った」は無害だが、人口調査では宣言が古くなった合図。
    """
    src = GUARD.read_text(encoding="utf-8")
    extra = src.replace(
        'CENSUS = frozenset({',
        'CENSUS = frozenset({\n    ("lib/nonexistent_module.py", "_gone"),', 1)
    assert extra != src, "CENSUS のアンカーが変わっている"
    tweaked = ROOT / "scripts" / "_census_probe_removed.py"
    tweaked.write_text(extra, encoding="utf-8")
    try:
        r = subprocess.run([sys.executable, str(tweaked)],
                           capture_output=True, text=True, cwd=str(ROOT))
    finally:
        tweaked.rename(ROOT / ".trash" / "_census_probe_removed.py.used")
    assert r.returncode != 0, (
        "台帳にあるのに見つからない箇所が検知されなかった:\n" + r.stdout + r.stderr)
    assert "台帳にあるが見つからない" in (r.stdout + r.stderr), r.stdout + r.stderr


def test_tests_are_not_counted():
    """tests/ の stub を数えないこと。

    テストは git 応答を差し替えるために同じ旗を書く。数えると守りたい状態
    (= 本番コードで何箇所が git にこの問いを立てるか) がぼやける。
    """
    src = GUARD.read_text(encoding="utf-8")
    assert '"tests"' not in src.split("SCAN_DIRS")[1].split(")")[0], (
        "tests/ が走査対象に入っている")
    # 実際に tests/ の中に stub が在る状態で緑であることを測る (静的確認だけでは
    # 「書いてある」止まりなので、実挙動でも確かめる)
    # `-e` が無いと grep が旗として解釈する (最初に書いた版は空振りして
    # 「stub が無い」と誤報した。空振りで緑にならない assert を置いていたので
    # 気づけた)。
    hits = subprocess.run(
        ["grep", "-rl", "-e", "--git-common-dir", "tests/"],
        capture_output=True, text=True, cwd=str(ROOT))
    assert hits.stdout.strip(), "tests/ に stub が無い = この試験が何も測っていない"
    assert _run_guard(ROOT).returncode == 0
