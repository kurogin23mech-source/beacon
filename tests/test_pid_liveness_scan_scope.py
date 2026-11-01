"""`check-pid-liveness.py` が見る範囲の試験 (ms-170 e-7249)。

**なぜ要るのか**: この検査はリポジトリ全体を歩く唯一のガードで、道具 (Claude Code)
の作業場所 `.claude/` まで降りていた。独立レビュアーを走らせると
`.claude/worktrees/agent-*` に作業コピーが残り、そこはリポジトリの**過去の状態**
そのものなので、今は直してある古い書き方がそのまま在る。歩くと「直したのに
指摘される」ので、毎回ノイズが出て、やがてこの検査の出力ごと読み流される
(= 緑も赤も信用されなくなる)。実際に 2026-11-01 に誤検知 3 件が出た。

**線引きの考え方**: 「レビュアーの作業コピーを除く」ではなく **リポジトリが実際に
運んでいるコードだけを見る**。前者だと置き場所が変わった瞬間に漏れるが、後者なら
道具が何を置いても関係ない。`.claude/` は git 管理下に 1 ファイルも無いので、
この線引きで落ちるのは「リポジトリのコードではないもの」だけになる。

ここで固定するのは 3 つ:

1. `.claude` が除外一覧に在ること
2. **その前提** — `.claude` が git 管理下でないこと (追跡され始めたら、除外は
   製品コードの盲点に変わるので、この試験が気づく)
3. **除外が実際に効くこと** — 同じ違反を除外先と走査先に置いて、前者だけが
   報告されないことを実測する (一覧に名前が在るだけでは、効いている証拠にならない)
"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
GUARD = ROOT / "scripts" / "check-pid-liveness.py"


def _load_guard():
    """ハイフン入りファイル名なので import 文では読めない。直接読み込む。"""
    spec = importlib.util.spec_from_file_location("check_pid_liveness", GUARD)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_pid_liveness"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_tool_workspace_is_excluded():
    """`.claude` (道具の作業場所) は走査から外れている。"""
    mod = _load_guard()
    assert ".claude" in mod.SKIP_DIRS


def test_the_premise_holds_claude_dir_is_not_tracked():
    """**除外してよい前提**: `.claude/` は git 管理下に無い。

    追跡され始めたら、この除外は製品コードの盲点に変わる。そのときここが赤く
    なって、除外を狭めるか allowlist に移すかを判断させる。
    """
    out = subprocess.run(["git", "ls-files", ".claude"], cwd=ROOT,
                         capture_output=True, text=True, timeout=60)
    tracked = [ln for ln in out.stdout.splitlines() if ln.strip()]
    assert tracked == [], (
        ".claude/ が git 管理下に入りました。走査から外したままだと製品コードの"
        f"盲点になります。追跡されているもの: {tracked[:5]}")


# この検査が見るのは「signal が 0 でないと **証明できない**」呼び出し。
# literal 1 は証明できるので対象外 — 最初その形で書いて、下の『走査先なら
# 報告される』が赤くなって気づいた (試料が外れていると、除外の試験が
# 「何も検出できていないだけ」になる)。
VIOLATION = (
    "import os\n"
    "\n"
    "def probe(pid):\n"
    "    os.kill(pid, 0)\n"
    "    return True\n"
)


def _run_guard(root: pathlib.Path):
    """`--root` で任意の木を走査させ ``(rc, 出力)`` を返す。"""
    r = subprocess.run(
        [sys.executable, str(GUARD), "--strict", "--root", str(root)],
        capture_output=True, text=True, timeout=300)
    return r.returncode, r.stdout + r.stderr


def test_a_violation_in_a_scanned_directory_is_reported(tmp_path):
    """走査先に置いた違反は報告される (= 試験そのものが効いていることの確認)。

    これが緑でないと、下の「除外先では報告されない」は『除外が効いた』ではなく
    『そもそも何も検出できていない』を意味してしまう。
    """
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "probe.py").write_text(VIOLATION, encoding="utf-8")
    rc, out = _run_guard(tmp_path)
    assert rc != 0, f"走査先の違反を見逃しました:\n{out}"
    assert "lib/probe.py" in out


def test_the_same_violation_under_the_tool_workspace_is_not_reported(tmp_path):
    """**同じ違反**を `.claude/` の下に置くと報告されない (除外が実際に効く)。

    一覧に名前が在ることと、歩かないことは別。上の試験と同じ中身を置き場所だけ
    変えて測るので、「検出できる違反を、置き場所の理由で見ていない」ことが言える。
    """
    nested = tmp_path / ".claude" / "worktrees" / "agent-deadbeef" / "lib"
    nested.mkdir(parents=True)
    (nested / "probe.py").write_text(VIOLATION, encoding="utf-8")
    rc, out = _run_guard(tmp_path)
    assert rc == 0, f".claude/ の下を歩いています:\n{out}"
    assert "agent-deadbeef" not in out


@pytest.mark.parametrize("skipped", sorted({".claude", ".worktrees", ".trash"}))
def test_each_excluded_directory_actually_stops_the_walk(tmp_path, skipped):
    """除外一覧の各ディレクトリで、歩かないことを 1 件ずつ測る。

    一覧はただの集合なので、1 つだけ効いていて残りが効かない形もありうる
    (実際に `.claude` が抜けていた)。名前ごとに実測する。
    """
    nested = tmp_path / skipped / "deep" / "lib"
    nested.mkdir(parents=True)
    (nested / "probe.py").write_text(VIOLATION, encoding="utf-8")
    rc, out = _run_guard(tmp_path)
    assert rc == 0, f"{skipped}/ の下を歩いています:\n{out}"


def test_repo_root_itself_stays_clean():
    """実リポジトリでこの検査が緑であること (回帰の受け皿)。"""
    rc, out = _run_guard(ROOT)
    assert rc == 0, out
    assert "ok" in out
