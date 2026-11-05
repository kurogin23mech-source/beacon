"""レビューの原典が「どの作業ツリーの版か」で黙って変わらないようにする (ms-119 e-7345)。

## 何が起きていたか

repo-file 原典は作業ツリーの中のファイルで、解決は
``os.path.dirname(os.path.dirname(os.path.abspath(__file__)))`` ——
**実行中の lib がある作業ツリー** を根にしていた。worktree から `beacon` を叩くと
install 解決の都合で main 側の lib が走るので、**自分の worktree の原典は配られず、
main の作業ツリーの版が配られる**。さらに main が pull していなければもっと古い版が
配られる。

**実測 (2026-11-05)**: AX 原典を 6 原則 → P1〜P8 に再構成した PR が origin/main に
入った直後、worktree から走らせたレビューに **216 行の旧原典** が配られていた
(worktree の実物は 437 行)。``gaps`` は空で、判定役も読み手も気づく手段が無い。
実害として、1 回のレビューで新しい観点が一度も当たらず、正しい原典で再実行したら
findings が 1 → 3 件に増えた。

## 採った規則

**レビュー対象のリポジトリを優先し、無ければ capability に落とし、どちらかを必ず言う。**

- 原典への変更それ自体がレビュー対象になりうる (原則を書き換える PR は、それが
  打ち立てる基準で判定されるべき) ので、対象リポが自分の基準の真値源。
- beacon 以外のリポをレビューする経路は壊さない (原典は capability と一緒に旅する
  設計なので、対象側に無ければ落ちるのが正しい)。
- **両方在って違うときは勝者を黙って選ばない。** ``gaps`` に出す。目的は「どちらを
  使うか」を決めることではなく、**使っていない基準を使ったと主張するレビューを止める**
  ことなので。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import commands  # noqa: E402
import review_spine  # noqa: E402

REF = "skills/ax-review/principles.md"


def _tree(root, text):
    d = root / "skills" / "ax-review"
    d.mkdir(parents=True)
    (d / "principles.md").write_text(text, encoding="utf-8")
    return root


def _roots(monkeypatch, reviewed, capability):
    monkeypatch.setattr(commands, "_reviewed_repo_root", lambda: reviewed)
    monkeypatch.setattr(commands, "_capability_root", lambda: capability)


def test_the_reviewed_repo_wins_and_the_divergence_is_disclosed(monkeypatch, tmp_path):
    rev = _tree(tmp_path / "reviewed", "NEW: P1 操作の語彙を閉じる\n")
    cap = _tree(tmp_path / "capability", "OLD: 6 原則\n")
    _roots(monkeypatch, str(rev), str(cap))

    gaps = []
    content, prov = commands._resolve_repo_file_origin(REF, gaps)

    assert "NEW" in content, "レビュー対象側の原典が配られていません"
    assert prov["resolved_from"] == "reviewed-repo"
    assert prov["path"] == str(rev / "skills" / "ax-review" / "principles.md")
    assert len(gaps) == 1, (
        "2 つの作業ツリーで原典が食い違っているのに gaps が空です。"
        "黙って勝者を選ぶと、使っていない基準で判定したレビューが見分けられません")
    assert REF in gaps[0]


def test_no_gap_when_the_two_trees_agree(monkeypatch, tmp_path):
    """同じ内容なら黙っていてよい (常に出る警告は読まれなくなる)。"""
    same = "SAME 原典\n"
    rev = _tree(tmp_path / "reviewed", same)
    cap = _tree(tmp_path / "capability", same)
    _roots(monkeypatch, str(rev), str(cap))

    gaps = []
    content, prov = commands._resolve_repo_file_origin(REF, gaps)
    assert content == same
    assert prov["resolved_from"] == "reviewed-repo"
    assert gaps == []


def test_falls_back_to_the_capability_for_a_repo_that_is_not_this_tool(monkeypatch, tmp_path):
    """beacon 以外のリポをレビューする経路を壊さない。"""
    cap = _tree(tmp_path / "capability", "CAP 原典\n")
    _roots(monkeypatch, str(tmp_path / "absent"), str(cap))

    gaps = []
    content, prov = commands._resolve_repo_file_origin(REF, gaps)
    assert content == "CAP 原典\n"
    assert prov["resolved_from"] == "capability"
    assert gaps == [], "落ちるのが正しい経路で警告を出すとノイズになります"


def test_falls_back_when_cwd_is_not_a_git_repo(monkeypatch, tmp_path):
    cap = _tree(tmp_path / "capability", "CAP 原典\n")
    _roots(monkeypatch, "", str(cap))
    content, prov = commands._resolve_repo_file_origin(REF, [])
    assert prov["resolved_from"] == "capability"
    assert content == "CAP 原典\n"


def test_missing_in_both_trees_exits_naming_where_it_looked(monkeypatch, tmp_path, capsys):
    """見つからないときは、探した場所を両方名乗る (回復経路を示す / P6)。"""
    _roots(monkeypatch, str(tmp_path / "a"), str(tmp_path / "b"))
    try:
        commands._resolve_repo_file_origin(REF, [])
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError("原典が無いのに成功しました")
    err = capsys.readouterr().err
    assert REF in err
    assert str(tmp_path / "b") in err, "探した場所が示されていません"


# --- bundle がどの版で判定したかを運ぶこと ---

def test_the_bundle_carries_which_tree_the_origin_came_from():
    """判定役と人間が、後から「どの版で判定したか」を確認できる。"""
    b = review_spine.assemble_review_context(
        "ax", origin_id=REF, origin_content="x", diff_text="d",
        mode="diff", target_ref="pr:1",
        origin_provenance={"resolved_from": "reviewed-repo", "path": "/w/" + REF})
    assert b["origin"]["resolved_from"] == "reviewed-repo"
    assert b["origin"]["path"] == "/w/" + REF


def test_the_origin_slot_keeps_its_old_shape_without_provenance():
    """doc 原典には作業ツリーが無いので、言えない呼び出し元は従来の形のまま。"""
    b = review_spine.assemble_review_context(
        "philosophy", origin_id="doc-1", origin_content="x", diff_text="d",
        mode="diff", target_ref="ms-1")
    assert b["origin"] == {"id": "doc-1", "content": "x"}


# --- 解決が 1 箇所であること (以前は 2 箇所に手書きで並んでいた) ---

def test_both_review_context_paths_share_one_resolution():
    """単一型の経路と batch の経路が、同じ解決を通ること。

    以前はそれぞれが同じ解決を手書きで持っており、片方だけ直る形だった
    (コード内の注記自身が「抽出は価値ある follow-up」と認めていた)。
    """
    import inspect
    src = inspect.getsource(commands)
    assert src.count("def _resolve_repo_file_origin(") == 1
    # 旧式の手書き解決 (install_root を自分で組んでファイルを開く形) が
    # 残っていないこと。
    assert "origin_path = os.path.join(install_root" not in src, (
        "原典の解決が再び手書きで複製されています。1 箇所に寄せた意味が消えます")
