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


# --- 範囲内で 2 回以上動いた場合 (独立 AX レビュー misleading) ---

def test_multi_hop_move_reports_only_the_path_that_exists_at_head(repo):
    """**本体**: 中間のパスを名指さない。出発点 → 最終の行き先の 1 行だけ。

    初版は rename の hop を 1 つずつ出していたので、範囲内で 2 回動いた
    ファイルでは **head にも差分にも存在しない中間パス** を名指す行が出た。
    文脈ゼロの judge はそれを探して見つけられず「この注記は信用できない」と
    読み、注記が守ろうとした『意図的な退役』の免罪ごと疑う —— この機能が
    消すはずだった誤指摘に戻る。
    """
    path, git = repo
    (path / "tests").mkdir()
    (path / "tests" / "y.py").write_text("y = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "create in tests/")
    git("mv", "tests/y.py", "tests/intermediate.py")
    git("commit", "-q", "-m", "rename once")
    (path / "trash").mkdir()
    git("mv", "tests/intermediate.py", "trash/y.py")
    git("commit", "-q", "-m", "retire to trash/")
    base, head = _base_head(path)

    moves = commands._intra_range_moves(base, head)
    # 1 行だけ。出発点は範囲に入る前の最初のパス。
    assert [(m["path"], m["from"]) for m in moves] == [
        ("trash/y.py", "tests/y.py")], moves
    # 中間パスは **どこにも現れない** (judge が探して見つけられないものを書かない)。
    ref = commands._artifact_move_reference(diff_ref=f"{base}...{head}")
    assert "intermediate" not in ref["content"], ref["content"]
    # 差分側でも中間は見えない (= 注記が差分と同じものを指している)。
    out = subprocess.run(["git", "diff", "--name-status", f"{base}..{head}"],
                         cwd=str(path), capture_output=True, text=True)
    assert "intermediate" not in out.stdout, out.stdout


def test_every_reported_destination_exists_at_head(repo):
    """注記が名指す行き先は必ず head に実在する (= 探して見つかる)。

    上の試験は 1 事例を固定するが、これは **全行** に掛ける性質。
    """
    path, git = repo
    (path / "a").mkdir()
    (path / "a" / "one.py").write_text("1\n", encoding="utf-8")
    (path / "a" / "two.py").write_text("2\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "create two")
    (path / "b").mkdir()
    git("mv", "a/one.py", "b/one.py")
    git("mv", "a/two.py", "a/two_mid.py")
    git("commit", "-q", "-m", "move both once")
    git("mv", "a/two_mid.py", "b/two.py")
    git("commit", "-q", "-m", "move two again")
    base, head = _base_head(path)
    moves = commands._intra_range_moves(base, head)
    assert moves, "何も検出していない (前提が崩れている)"
    for m in moves:
        ok = subprocess.run(["git", "cat-file", "-e", f"{head}:{m['path']}"],
                            cwd=str(path), capture_output=True)
        assert ok.returncode == 0, f"head に無いパスを名指している: {m}"


# --- 差分と注記が同じ sha を基準にすること (独立 AX レビュー silent) ---

def test_note_uses_the_sha_the_diff_was_pinned_to(monkeypatch, repo):
    """呼び出し側が解決済みの sha を渡したら、それを基準にする。

    初版は注記のために `gh pr view` を**別に**叩いて head を引き直していたので、
    差分採取との間に push が入ると「judge に渡る差分は古い head、注記は新しい
    head」という食い違いが silent に起きた (e-6859 が直した型の再導入)。
    """
    path, git = repo
    (path / "tests").mkdir()
    (path / "tests" / "y.py").write_text("y = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "create")
    git("mv", "tests/y.py", "y.py")
    git("commit", "-q", "-m", "move")
    base, head = _base_head(path)

    # gh は「別の head」を返す (= 読んでいる間に push された状況)。
    def fake_git_ok(argv):
        if argv[:3] == ["gh", "pr", "view"]:
            return True, json.dumps({"baseRefOid": base,
                                     "headRefOid": "f" * 40})
        return commands._git_ok(argv)

    real = commands._git_ok
    monkeypatch.setattr(commands, "_git_ok",
                        lambda argv: fake_git_ok(argv) if argv[:3] == ["gh", "pr", "view"]
                        else real(argv))
    gaps = []
    ref = commands._artifact_move_reference(pr="1", head_sha=head, gaps=gaps)
    # 注記は **渡された sha** を基準に組まれる (= 差分と同じものを見ている)。
    assert ref is not None and "tests/y.py" in ref["content"], ref
    # 食い違いは隠さず開示する。
    assert any("head" in g and head[:12] in g for g in gaps), gaps


def test_no_gap_when_the_two_shas_agree(monkeypatch, repo):
    """一致しているときに警告を出さない (正常時にノイズを出さない)。"""
    path, git = repo
    (path / "tests").mkdir()
    (path / "tests" / "y.py").write_text("y = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "create")
    git("mv", "tests/y.py", "y.py")
    git("commit", "-q", "-m", "move")
    base, head = _base_head(path)
    real = commands._git_ok
    monkeypatch.setattr(
        commands, "_git_ok",
        lambda argv: ((True, json.dumps({"baseRefOid": base, "headRefOid": head}))
                      if argv[:3] == ["gh", "pr", "view"] else real(argv)))
    gaps = []
    ref = commands._artifact_move_reference(pr="1", head_sha=head, gaps=gaps)
    assert ref is not None, ref
    assert gaps == [], gaps


# --- 範囲内で元の場所へ戻る循環 (独立保守性レビュー high, origin=introduced) ---
#
# `tests/y.py → trash/y.py → tests/y.py` のように **退避してから同じ範囲内で元へ
# 戻す** 形は、このリポジトリの日常操作 (`.trash/` へ退避 → やめて戻す)。初版は
# 遡りで訪問済みを覚えていなかったので連鎖の輪を回り続け、`guard` 上限到達時点の
# **偶奇依存の値** を「出発点」として報告した。その値は base にも head にも無い
# 中間パスでありうる。
#
# 最悪の形は **`git diff base..head` が完全に空 (= 何も変わっていない) のに
# 「移動した」注記が出る** こと —— judge に的外れな弁明を手渡し、この機構が消そうと
# した誤読を逆向きに再生産する。
#
# 初版の試験はここを覆えていなかった: `path` (行き先) が head に実在することは
# 全行に掛けていたが、**`from` (由来) の正しさを一度も見ていなかった**。

def test_round_trip_move_is_not_reported(repo):
    """**本体**: 退避して元に戻したら注記しない (添える履歴が無い)。"""
    path, git = repo
    (path / "tests").mkdir()
    (path / "tests" / "y.py").write_text("y = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "base has tests/y.py")
    base_with_file = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(path),
                                    capture_output=True, text=True).stdout.strip()
    (path / "trash").mkdir()
    git("mv", "tests/y.py", "trash/y.py")
    git("commit", "-q", "-m", "retire")
    git("mv", "trash/y.py", "tests/y.py")
    git("commit", "-q", "-m", "restore")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(path),
                          capture_output=True, text=True).stdout.strip()
    # 前提: 範囲の差分は空 (何も変わっていない)。
    out = subprocess.run(["git", "diff", "--name-status", f"{base_with_file}..{head}"],
                         cwd=str(path), capture_output=True, text=True)
    assert out.stdout.strip() == "", out.stdout
    assert commands._intra_range_moves(base_with_file, head) == []
    assert commands._artifact_move_reference(
        diff_ref=f"{base_with_file}...{head}") is None


def test_three_node_cycle_is_not_reported(repo):
    """3 つ巴の循環でも、存在しない中間パスを由来として出さない。"""
    path, git = repo
    for d in ("a", "b", "c"):
        (path / d).mkdir()
    (path / "a" / "f.py").write_text("f = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "base has a/f.py")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(path),
                          capture_output=True, text=True).stdout.strip()
    git("mv", "a/f.py", "b/f.py")
    git("commit", "-q", "-m", "a->b")
    git("mv", "b/f.py", "c/f.py")
    git("commit", "-q", "-m", "b->c")
    git("mv", "c/f.py", "a/f.py")
    git("commit", "-q", "-m", "c->a")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(path),
                          capture_output=True, text=True).stdout.strip()
    moves = commands._intra_range_moves(base, head)
    # 中間パス (b/ c/) を由来として名指さない。
    assert all("b/f.py" != m["from"] and "c/f.py" != m["from"] for m in moves), moves
    # 元の場所へ戻っているので、そもそも注記しない。
    assert moves == [], moves


def test_every_reported_source_is_absent_at_base(repo):
    """**全行に掛ける性質**: 報告する由来は base に存在しない。

    これが規則そのもの (base に在るなら差分が rename として見せるので注記は不要)。
    初版の試験は行き先の実在だけを全行に掛けており、**由来の側を一度も見ていな
    かった** —— そこが循環の欠陥を通した穴だった。
    """
    path, git = repo
    # base に在るファイルの移動 (注記しないべき) と、範囲内で作って移したファイル
    # (注記するべき) を混ぜる。
    (path / "src").mkdir()
    (path / "src" / "new.py").write_text("n = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "create new.py inside range")
    (path / "dst").mkdir()
    git("mv", "src/new.py", "dst/new.py")
    git("mv", "keep.py", "dst/keep.py")
    git("commit", "-q", "-m", "move both")
    base, head = _base_head(path)
    moves = commands._intra_range_moves(base, head)
    assert moves, "何も検出していない (前提が崩れている)"
    for m in moves:
        at_base = subprocess.run(["git", "cat-file", "-e", f"{base}:{m['from']}"],
                                 cwd=str(path), capture_output=True)
        assert at_base.returncode != 0, f"base に在る由来を報告している: {m}"
    # base に在った keep.py の移動は注記されない。
    assert all(m["from"] != "keep.py" for m in moves), moves


def test_cycle_of_a_file_created_inside_the_range_is_not_reported(repo):
    """循環 **かつ** 出発点が base に無い場合も報告しない。

    **なぜこの 1 本が要るか** (自分の反転で判明): 上の 2 本 (退避→復元 / 3 つ巴) は
    どちらも出発点が base に在るので、循環を検出し損ねても **base フィルタが
    偶然捕まえる**。そのため「循環を検出したら落とす」の行を殺しても緑のままだった
    = 私の試験がその行を固定できていなかった (偽の安全)。

    範囲内で作られたファイルが循環すると base フィルタは効かない: 出発点が base に
    無いので通ってしまい、`{path: a, from: a}` のような **自分自身を由来とする**
    無意味な注記が出る。ここを固定する。
    """
    path, git = repo
    (path / "a").mkdir()
    (path / "b").mkdir()
    (path / "a" / "made.py").write_text("m = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "create inside range")
    git("mv", "a/made.py", "b/made.py")
    git("commit", "-q", "-m", "a->b")
    git("mv", "b/made.py", "a/made.py")
    git("commit", "-q", "-m", "b->a (cycle, origin absent at base)")
    base, head = _base_head(path)
    # 前提: 出発点は base に無い (= base フィルタは効かない)。
    at_base = subprocess.run(["git", "cat-file", "-e", f"{base}:a/made.py"],
                             cwd=str(path), capture_output=True)
    assert at_base.returncode != 0, "前提が崩れている (base に在る)"
    moves = commands._intra_range_moves(base, head)
    # 自分自身を由来とする注記を出さない。
    assert all(m["path"] != m["from"] for m in moves), moves
    # 元の場所へ戻っているので、そもそも注記しない。
    assert moves == [], moves
