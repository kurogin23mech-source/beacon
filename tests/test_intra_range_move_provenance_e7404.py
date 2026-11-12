"""差分の範囲内で移動してきたファイルを judge に伝えることの固定 (ms-119 / e-7404)。

**何が起きていた** (PR#840 の 6 周目, 2026-11-10): `.trash/` へ退避した試験ファイル
について、独立 judge 2 体が揃って high で「新しく書いた現役の試験を置き間違えた、
tests/ へ戻すべき」と指摘した。このリポジトリは ``rm`` を使わず ``.trash/`` へ
``mv`` して退役させる運用なので、退役のたびに構造的に再発する。しかも全 judge が
同じ入力を読むので **2 体一致が誤りを強める方向に働き、合意度では濾せない**。

**起票時の見立ては実測で否定された** (2026-11-12): 起票は原因を「差分採取で rename
検出 (git の ``-M``) が効いていない」としていたが、測ると違った。

* ``git show --format= -U15 -m --first-parent fa76e7e4`` は **既に rename を検出**
  しており (``rename from`` 1 件)、``-M`` を足すと **バイト単位で同一** (27,065 →
  27,065) だった。``diff.renames`` は git 2.9 以降の既定が true。
* ``gh pr diff 840`` も rename を 4 件検出していた。
* PR#840 の base ``ee8ddcc5`` に元パス ``tests/…e7248.py`` は **存在しない**。
  ``git diff -M`` でも ``--no-renames`` でも判定は同じ ``A`` (新規追加)。

**真の機構**: 当該ファイルは **同じ PR の中で作られ (6e88aac0)、同じ PR の中で退避
された (fa76e7e4)**。base に元パスが無いので、base..head の差分では移動先への純粋な
新規追加になる。rename 検出では復元できない —— rename の相手が base に居ないため。
judge の読みは **渡された差分に対しては正しかった**。欠けていたのは範囲内の履歴。

**この module が測るもの**: 散文ではなく、(1) どの範囲で注記を出すかの判定規則、
(2) 注記が bundle の judge 向け参照に実際に載ること。
"""

import json
import os
import subprocess
import sys
import pathlib

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import commands  # noqa: E402
import review_spine as rs  # noqa: E402


# --- 純粋な整形 (git を触らない) ---

def test_no_moves_yields_no_reference():
    """移動が無ければ参照を載せない (空の参照で「確認した」に見せない)。"""
    assert rs.build_intra_range_move_reference([]) is None
    assert rs.build_intra_range_move_reference(None) is None
    # path / from のどちらかを欠く行は数えない。
    assert rs.build_intra_range_move_reference([{"path": "a"}]) is None


def test_reference_names_both_ends_and_the_commit():
    """注記は移動先・元パス・移動した commit を名指す (judge が辿れる形)。"""
    ref = rs.build_intra_range_move_reference(
        [{"path": ".trash/x/y.py", "from": "tests/y.py", "commit": "deadbee"}])
    assert ref["kind"] == "artifact-provenance"
    for needle in (".trash/x/y.py", "tests/y.py", "deadbee"):
        assert needle in ref["content"], ref["content"]
    # 生データも運ぶ (judge でなく機械が読む消費者のため)。
    assert ref["moves"][0]["from"] == "tests/y.py"


# --- 判定規則 (実物の git リポジトリで測る) ---

@pytest.fixture
def repo(tmp_path, monkeypatch):
    """使い捨ての git リポジトリを作り、cwd をそこへ移す。"""
    def git(*args):
        subprocess.run(["git", *args], cwd=str(tmp_path), check=True,
                       capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "keep.py").write_text("x = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    monkeypatch.chdir(tmp_path)
    return tmp_path, git


def _base_head(repo_path):
    out = subprocess.run(["git", "log", "--format=%H", "--reverse"],
                         cwd=str(repo_path), capture_output=True, text=True)
    shas = out.stdout.split()
    return shas[0], shas[-1]


def test_created_then_moved_inside_the_range_is_reported(repo):
    """**本体**: 範囲内で作られ、その後移されたファイルは注記に出る。

    これが PR#840 で誤読された形そのもの (base に元パスが無いので、差分は移動先への
    新規追加に見える)。
    """
    path, git = repo
    (path / "tests").mkdir()
    (path / "tests" / "y.py").write_text("y = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "create in tests/")
    (path / "trash").mkdir()
    git("mv", "tests/y.py", "trash/y.py")
    git("commit", "-q", "-m", "retire to trash/")
    base, head = _base_head(path)

    moves = commands._intra_range_moves(base, head)
    assert [(m["path"], m["from"]) for m in moves] == [
        ("trash/y.py", "tests/y.py")], moves
    assert moves[0]["commit"], moves


def test_move_of_a_file_that_existed_at_base_is_not_reported(repo):
    """元パスが base に在る移動は注記しない (差分が rename として見せるので不要)。

    注記を出し過ぎると、judge が読む参照がノイズになる。**注記は差分だけでは
    辿れない場合に限る**。
    """
    path, git = repo
    moves_before = commands._intra_range_moves(*_base_head(path))
    assert moves_before == []
    git("mv", "keep.py", "kept.py")
    git("commit", "-q", "-m", "rename a file that existed at base")
    base, head = _base_head(path)
    # 差分は rename として見える (= judge は履歴の補いを要らない)。
    out = subprocess.run(["git", "diff", "--name-status", f"{base}..{head}"],
                         cwd=str(path), capture_output=True, text=True)
    assert out.stdout.startswith("R"), out.stdout
    assert commands._intra_range_moves(base, head) == []


def test_unresolvable_range_yields_nothing(repo):
    """範囲の端が手元に無ければ空を返す (推測で注記を捏造しない)。"""
    path, _git = repo
    base, head = _base_head(path)
    assert commands._intra_range_moves("", head) == []
    assert commands._intra_range_moves(base, "") == []
    assert commands._intra_range_moves("0" * 40, head) == []


def test_move_with_content_change_still_reported(repo):
    """移動しながら中身も変えた場合も注記に出る (受入条件2: 欠落させない)。"""
    path, git = repo
    (path / "src").mkdir()
    (path / "src" / "z.py").write_text("\n".join(f"line {i}" for i in range(40)),
                                       encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "create")
    (path / "dst").mkdir()
    git("mv", "src/z.py", "dst/z.py")
    (path / "dst" / "z.py").write_text(
        "\n".join(f"line {i}" for i in range(40)) + "\nextra\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "retire and edit")
    base, head = _base_head(path)
    moves = commands._intra_range_moves(base, head)
    assert [(m["path"], m["from"]) for m in moves] == [
        ("dst/z.py", "src/z.py")], moves
    # 変更内容そのものは差分側に残る (注記は履歴を添えるだけで、差分を置き換えない)。
    out = subprocess.run(["git", "diff", f"{base}..{head}"],
                         cwd=str(path), capture_output=True, text=True)
    assert "extra" in out.stdout


# --- 配線 (両経路が同じ入口を通る) ---

def test_both_collection_paths_call_the_single_entry():
    """単一種別の経路と --batch の経路が、同じ 1 つの入口を通ること。

    片方だけに足すと、節目のバッチ発火 (= judge を実際に起動する既定経路) で
    効かない形が作れる。呼び出し元の集合を構文木で数えて固定する。
    """
    import ast
    src = (pathlib.Path(__file__).resolve().parents[1] / "lib" / "commands.py"
           ).read_text(encoding="utf-8")
    tree = ast.parse(src)
    callers = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Name)
                    and sub.func.id == "_artifact_move_reference"):
                callers.add(node.name)
    assert callers == {"cmd_review_context", "cmd_review_batch_context"}, callers


def test_diff_ref_path_resolves_the_range(repo):
    """--diff-ref 経路でも範囲が割れて注記が出ること。"""
    path, git = repo
    (path / "tests").mkdir()
    (path / "tests" / "y.py").write_text("y = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "create")
    git("mv", "tests/y.py", "y.py")
    git("commit", "-q", "-m", "move to root")
    base, head = _base_head(path)
    ref = commands._artifact_move_reference(diff_ref=f"{base}...{head}")
    assert ref is not None and "tests/y.py" in ref["content"], ref
    # 2 点ドットでも割れる。
    ref2 = commands._artifact_move_reference(diff_ref=f"{base}..{head}")
    assert ref2 is not None and "tests/y.py" in ref2["content"], ref2
