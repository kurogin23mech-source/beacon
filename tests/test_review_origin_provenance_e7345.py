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
import json
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


# ---------------------------------------------------------------------------
# 独立レビュー 2 体の合意指摘 — 全呼び出し元が開示を受け取ること
# ---------------------------------------------------------------------------
#
# 原典を解決する口は 3 つから呼ばれる (単一型 / batch / decision-verification)。
# 初版は 3 番目だけ ``gaps`` を渡さず、既定値 ``gaps=None`` が使い捨ての空リストを
# 作って **食い違いの開示をその場で捨てていた**。provenance も受け取るだけで
# bundle に渡していなかった。つまり「使っていない基準で判定したことを隠せない
# ようにする」という目的が、1 経路だけで完全に無効化されていた —— しかもクラッシュ
# せず黙って動く。独立 2 体が別々に見つけ、どちらも実行して確認している。
#
# だから「渡し忘れ」を **物理的に不可能** にした (``gaps`` を必須の位置引数に) 上で、
# 3 経路それぞれが実際に開示を受け取ることを振る舞いで測る。4 番目の呼び出し元が
# 増えたとき、黙って抜けられないようにするため。

def test_gaps_is_a_required_argument_so_a_caller_cannot_silently_skip_it():
    """渡し忘れたら落ちる。既定値で飲み込む形に戻していないこと。"""
    import inspect
    sig = inspect.signature(commands._repo_file_origin)
    gaps_param = sig.parameters["gaps"]
    assert gaps_param.default is inspect.Parameter.empty, (
        "gaps に既定値が戻っています。既定値があると、渡し忘れた呼び出し元は "
        "解決だけ受け取って開示を黙って失います (初版で実際に起きた形)")


def test_the_decision_verification_path_discloses_the_divergence(monkeypatch, tmp_path, capsys):
    """3 番目の呼び出し元が、食い違いの開示と出所を bundle に載せること。"""
    ref = "skills/decision-verification/principles.md"
    rev = tmp_path / "reviewed"
    cap = tmp_path / "capability"
    for root, text in ((rev, "NEW 原典\n"), (cap, "OLD 原典\n")):
        (root / "skills" / "decision-verification").mkdir(parents=True)
        (root / ref).write_text(text, encoding="utf-8")
    monkeypatch.setattr(commands, "_reviewed_repo_root", lambda: str(rev))
    monkeypatch.setattr(commands, "_capability_root", lambda: str(cap))
    monkeypatch.setattr(review_spine, "load_review_types", lambda: {
        "decision-verification": {"id": "decision-verification", "judge_run": True,
                                  "origin": {"kind": "repo-file", "ref": ref}}})
    monkeypatch.setenv("BEACON_DECISION_KIND", "")
    monkeypatch.setenv("BEACON_DECISION_LIMIT", "1")

    commands._emit_decision_verification_context()
    out = json.loads(capsys.readouterr().out)

    assert out["origin"]["resolved_from"] == "reviewed-repo", (
        "decision-verification の bundle に出所が載っていません。どの版で判定したか "
        "後から確認できません")
    assert any("内容が 2 つの作業ツリーで異なります" in g for g in out["gaps"]), (
        "原典が食い違っているのに開示が bundle に届いていません。"
        f"gaps={out['gaps']}")


# ---------------------------------------------------------------------------
# 独立レビュー (AX) の指摘 — 本物の根の実装を測る
# ---------------------------------------------------------------------------
#
# 上のテスト群は 2 つの根を monkeypatch で差し替えるので、**本物の実装は一度も
# 動かない**。独立レビューが実測した: ``_capability_root`` の ``dirname`` を 1 段
# 浅くする (本番なら常に誤った根を指す重大な回帰) と、それでも全件緑だった。
# 反転ハーネスも同じテストを走らせるだけなので、同じ盲点を引き継いでいた。
# だから本物の 2 つを monkeypatch 無しで測る。

def test_the_real_capability_root_points_at_the_checkout(tmp_path):
    """本物の ``_capability_root`` が、lib/ の親 (= capability の根) を指すこと。

    ``dirname`` の段数を間違えても「それらしいパス」が返るので、パス文字列の
    比較だけでは弱い。**根であることの性質** (そこに skills/ と lib/ が在る) で測る。
    """
    root = commands._capability_root()
    assert os.path.isdir(os.path.join(root, "skills")), (
        f"{root} に skills/ がありません。原典は skills/<type>/ の下にあるので、"
        f"この根が間違っていると原典は永久に見つかりません")
    assert os.path.isdir(os.path.join(root, "lib")), f"{root} に lib/ がありません"
    assert os.path.samefile(
        os.path.join(root, "lib", "commands.py"),
        os.path.abspath(commands.__file__).replace(".pyc", ".py")), (
        "この根の lib/commands.py が、いま走っている commands.py と別物です")


def test_the_real_capability_root_has_one_definition():
    """式の写しを増やしていないこと (重複を畳む変更が重複を増やさない)。"""
    assert commands._capability_root() == review_spine.install_root()


def test_the_real_reviewed_repo_root_returns_the_git_toplevel(tmp_path, monkeypatch):
    """本物の ``_reviewed_repo_root`` が、実際の git リポジトリの頂点を返すこと。"""
    import subprocess
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    init = subprocess.run(["git", "init", "-q", str(repo)], capture_output=True)
    if init.returncode != 0:
        import pytest
        pytest.skip("git init が使えません")
    monkeypatch.chdir(repo / "sub")
    got = commands._reviewed_repo_root()
    assert got, "git リポジトリの中なのに空が返りました"
    assert os.path.samefile(got, str(repo)), f"got={got!r} want={str(repo)!r}"


def test_the_real_reviewed_repo_root_is_empty_outside_git(tmp_path, monkeypatch):
    """git 管理外では空を返し、capability へ落ちられること。"""
    outside = tmp_path / "plain"
    outside.mkdir()
    monkeypatch.chdir(outside)
    assert commands._reviewed_repo_root() == ""


# ---------------------------------------------------------------------------
# 独立レビュー (AX) の指摘 — 「在るのに読めない」を「無い」と言わない
# ---------------------------------------------------------------------------

def test_unreadable_is_disclosed_instead_of_being_called_absent(monkeypatch, tmp_path):
    """読めなかったことを、黙って capability への fallback に畳まないこと。"""
    rev = _tree(tmp_path / "reviewed", "NEW\n")
    cap = _tree(tmp_path / "capability", "CAP\n")
    target = rev / "skills" / "ax-review" / "principles.md"
    os.chmod(target, 0)
    try:
        _roots(monkeypatch, str(rev), str(cap))
        gaps = []
        content, prov = commands._resolve_repo_file_origin(REF, gaps)
        if content == "NEW\n":                      # root で走ると読めてしまう
            import pytest
            pytest.skip("この環境では chmod 0 でも読めます (root 実行)")
        assert prov["resolved_from"] == "capability"
        assert any("読めませんでした" in g for g in gaps), (
            "在るのに読めなかったことが開示されていません。『無いので capability を"
            f"使った』という体で進みます: gaps={gaps}")
    finally:
        os.chmod(target, 0o644)


def test_the_final_error_does_not_claim_not_found_when_it_could_not_read(monkeypatch, tmp_path, capsys):
    """両方読めないとき、実際に捕まえた理由を名乗ること (誤診文を出さない)。"""
    rev = _tree(tmp_path / "reviewed", "A\n")
    cap = _tree(tmp_path / "capability", "B\n")
    for root in (rev, cap):
        os.chmod(root / "skills" / "ax-review" / "principles.md", 0)
    try:
        _roots(monkeypatch, str(rev), str(cap))
        try:
            commands._resolve_repo_file_origin(REF, [])
        except SystemExit:
            err = capsys.readouterr().err
            if "読めませんでした" not in err:
                import pytest
                pytest.skip("この環境では chmod 0 でも読めます (root 実行)")
            assert "not found" not in err, (
                f"在るのに『見つかりません』と名乗っています: {err}")
        else:
            import pytest
            pytest.skip("この環境では chmod 0 でも読めます (root 実行)")
    finally:
        for root in (rev, cap):
            os.chmod(root / "skills" / "ax-review" / "principles.md", 0o644)
