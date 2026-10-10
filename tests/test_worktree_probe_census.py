"""worktree 判定の人口調査ガードが、増減の**両方**で赤くなることを測る (e-6932)。

このガードは「宣言ではなく機械で閉じる」ための道具なので、**それ自身が壊れたときに
赤くなる**ことを測らないと意味がない。緑のガードは読み手に信頼されるので、偽の安全は
ガード無しより悪い (CORE doc `jnFsoNaNvuZegI73noCn`)。

免除リスト型のガードは「増えたら赤」だけを測りがちだが、これは **人口調査 (census)**
なので減っても赤くなる必要がある (集合から消えた = 宣言が古くなった、も検知対象)。
だから両方向を測る。
"""
from __future__ import annotations

import re
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


def _probe_tree(tmp_path: Path, body: str, pkg: str = "lib") -> Path:
    """``--root`` で渡せる偽ツリーを作る (repo 本体に書かないための足場)。

    最初の版は本物の `lib/` にファイルを置いて実行していた。ガードの試験が repo を
    汚す形は、スイート自身の汚染検出が捕まえる対象でもあるので `--root` に寄せた。
    """
    d = tmp_path / pkg
    d.mkdir(parents=True, exist_ok=True)
    (d / "_probe_site.py").write_text(body, encoding="utf-8")
    return tmp_path


def _run_with_root(root: Path, *, strict: bool = True):
    cmd = [sys.executable, str(GUARD), "--root", str(root)]
    if strict:
        cmd.insert(2, "--strict")
    return subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT))


def _added_sites(proc) -> set:
    """ガードの出力から「台帳に無い」分だけを取り出す。

    ``--root`` を使うと本物の台帳 4 件は当然「見つからない」側に出るので、
    **exit code では検知の有無を測れない** (最初の版はそれで測っており、プローブを
    置かなくても非 0 になるため『正しい理由で緑』になっていなかった)。増えた分だけを
    見る。
    """
    out = proc.stdout + proc.stderr
    return {m.group(1).strip()
            for m in re.finditer(r"\+ 台帳に無い: (.+)", out)}


# --- 検知すべき「すり抜け方」を 1 形ずつ固定する (AX / 保守性レビュー PR #791) ---
#
# 最初の版はリテラルのリストを渡す形だけを試験しており、**引数リストを変数に出す
# ごく普通の整理で検知対象が黙って消える**ことを捕まえられなかった (保守性 high)。
# 「ガードを書いた」と「そのガードが抜け道を塞いでいる」は別で、抜け道の形ごとに
# 赤くなるかを測るまで信用できない。

def test_a_literal_call_is_detected(tmp_path):
    """素直な形: リテラルのリストで渡す。"""
    root = _probe_tree(tmp_path,
        "import subprocess\n"
        "def _plain():\n"
        "    return subprocess.run(['git', 'rev-parse', '--git-common-dir'])\n")
    r = _run_with_root(root)
    added = _added_sites(r)
    assert any("_plain" in a for a in added), (
        "検知されなかった (追加分: %s)\n%s" % (sorted(added), r.stdout + r.stderr))

def test_an_arg_list_moved_into_a_variable_is_detected(tmp_path):
    """**保守性 high の形**: 引数リストを変数に出しても消えないこと。"""
    root = _probe_tree(tmp_path,
        "import subprocess\n"
        "def _via_variable():\n"
        "    args = ['git', 'rev-parse', '--git-common-dir']\n"
        "    return subprocess.run(args)\n")
    r = _run_with_root(root)
    added = _added_sites(r)
    assert any("_via_variable" in a for a in added), (
        "検知されなかった (追加分: %s)\n%s" % (sorted(added), r.stdout + r.stderr))

def test_a_single_shell_string_is_detected(tmp_path):
    """シェル形 (1 本の文字列を shell=True で渡す) でも消えないこと。"""
    root = _probe_tree(tmp_path,
        "import subprocess\n"
        "def _via_shell():\n"
        "    return subprocess.run('git rev-parse --git-common-dir', shell=True)\n")
    r = _run_with_root(root)
    added = _added_sites(r)
    assert any("_via_shell" in a for a in added), (
        "検知されなかった (追加分: %s)\n%s" % (sorted(added), r.stdout + r.stderr))

def test_a_renamed_wrapper_is_detected(tmp_path):
    """呼び出し先が subprocess でなくても (自作ラッパ) 消えないこと。

    4 箇所が別々のラッパを使っているので、呼び出し先の名前で数えるとラッパを
    増やした人がすり抜ける。渡している中身で数えている契約をここで固定する。
    """
    root = _probe_tree(tmp_path,
        "def _my_runner(cmd):\n"
        "    return cmd\n"
        "def _via_wrapper():\n"
        "    return _my_runner(['git', 'rev-parse', '--git-dir'])\n")
    r = _run_with_root(root)
    added = _added_sites(r)
    assert any("_via_wrapper" in a for a in added), (
        "検知されなかった (追加分: %s)\n%s" % (sorted(added), r.stdout + r.stderr))

def test_a_different_question_is_not_counted(tmp_path):
    """別の問い (``--is-inside-work-tree``) は数えないこと。

    緩すぎる側の歯止め。曖昧な形を全部数える規則を一度入れたら、repo 全体の
    ``git rev-parse`` 15 件を巻き込んで守りたい集合がぼやけた。
    """
    root = _probe_tree(tmp_path,
        "import subprocess\n"
        "def _other_question():\n"
        "    return subprocess.run(['git', 'rev-parse', '--is-inside-work-tree'])\n")
    r = _run_with_root(root)
    added = _added_sites(r)
    assert added == set(), (
        "別の問いを worktree 判定として数えた (追加分: %s)" % sorted(added))

def test_a_probe_result_variable_does_not_poison_other_functions(tmp_path):
    """結果を受ける変数が、別の関数の同名変数を巻き込まないこと。

    名前解決を無差別にした版で実測した誤検知: ``r = subprocess.run([...旗...])``
    の ``r`` が「旗を持つ」と記録され、スコープを区別しないため別の関数の ``r`` が
    それを引き込んで無関係な行を数えた。解決対象を文字列リテラルの入れ物に限った。
    """
    root = _probe_tree(tmp_path,
        "import subprocess\n"
        "def _the_real_probe():\n"
        "    r = subprocess.run(['git', 'rev-parse', '--git-dir'])\n"
        "    return r.returncode == 0\n"
        "def _unrelated():\n"
        "    r = {'date': '2026-10-10'}\n"
        "    return str(r)\n")
    added = _added_sites(_run_with_root(root))
    assert any("_the_real_probe" in a for a in added), (
        "本物の判定を見逃した (追加分: %s)" % sorted(added))
    assert not any("_unrelated" in a for a in added), (
        "結果変数の名前が別の関数を巻き込んだ (追加分: %s)" % sorted(added))


def test_unknown_flags_are_refused(tmp_path):
    """未知の旗を無言で素通りさせないこと (AX high)。

    最初の版は ``main(argv)`` を取りながら argv を読まず、``--strict`` / ``--help`` /
    架空の旗のどれを渡しても同じ出力を返していた。姉妹ガード check-pid-liveness.py は
    同名の ``--strict`` で exit code を分岐させるので、同名で別の意味を持つと
    そちらから学んだ挙動モデルが外れる。
    """
    r = subprocess.run([sys.executable, str(GUARD), "--nonexistent-flag"],
                       capture_output=True, text=True, cwd=str(ROOT))
    assert r.returncode == 2, (
        "未知の旗が受け入れられた (EXIT=%s):\n%s" % (r.returncode, r.stdout + r.stderr))
    h = subprocess.run([sys.executable, str(GUARD), "--help"],
                       capture_output=True, text=True, cwd=str(ROOT))
    assert h.returncode == 0 and "--strict" in h.stdout, h.stdout + h.stderr


def test_strict_decides_the_exit_code(tmp_path):
    """``--strict`` が姉妹ガードと同じ意味 (既定は警告、strict で落ちる) を持つこと。"""
    root = _probe_tree(tmp_path,
        "import subprocess\n"
        "def _plain():\n"
        "    return subprocess.run(['git', 'rev-parse', '--git-common-dir'])\n")
    lax = subprocess.run([sys.executable, str(GUARD), "--root", str(root)],
                         capture_output=True, text=True, cwd=str(ROOT))
    strict = _run_with_root(root)
    assert lax.returncode == 0, "既定で落ちている (姉妹と意味が違う)"
    assert strict.returncode != 0, "--strict で落ちない (CI の関門にならない)"
    assert any("_plain" in a for a in _added_sites(lax)), "既定でも報告は出るべき"


def test_the_guidance_names_only_the_docstring_that_declares_a_count(tmp_path):
    """案内文が、件数を宣言している説明文だけを名指しすること (AX misleading)。

    最初の版は「3 箇所の docstring も直せ」と prose で書いており、同じファイルの
    CENSUS 注記 (「件数を宣言しているのは 1 つだけ」) と矛盾していた。CI で落ちた人に
    存在しない宣言を探させる形だったので、データから組み立てるようにした。
    """
    root = _probe_tree(tmp_path,
        "import subprocess\n"
        "def _plain():\n"
        "    return subprocess.run(['git', 'rev-parse', '--git-common-dir'])\n")
    out = _run_with_root(root).stderr
    assert "_worktree_shared_base" in out, (
        "件数を宣言している説明文が名指しされていない:\n" + out)
    assert "3 箇所の docstring" not in out, (
        "台帳と矛盾する prose が残っている:\n" + out)


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
        r = subprocess.run([sys.executable, str(tweaked), "--strict"],
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
