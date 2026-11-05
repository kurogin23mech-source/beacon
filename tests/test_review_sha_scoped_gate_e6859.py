"""レビュー実施の帳簿が「どの差分に対して実施したか」を持つことの試験
(ms-119 e-6859)。

何が起きていたか
----------------
``beacon-review-gate`` が緑なのに、**現在のコミットを独立レビューが一度も見て
いない** 状態が作れた。

義務は PR-open で 1 回だけトリガーファイルに書かれ、``beacon review done`` が
それを消す —— つまり「要る / 要らない」を **削除と復活の状態遷移** で表していた。
新しいコミットを積んでも復活させる経路が無かったので、一度実施すれば以後どれだけ
積んでも帳簿は「実施済み」のままだった。

実測:

* PR #785 (2026-10-02〜05): AX と保守性を head 7af4951f に対して実施 → 両方 done。
  10-05 に 4 コミット追加 (head 5d0adf6d)。保守性だけ再実施 (AX は judge 起動時の
  タイムアウトで 2 回失敗し未実施)。結果、関門は「独立レビュー記録済み」で pass。
  AX は追加の 4 コミットを一度も見ていない。
* PR #834 / #836 / #835: レビューを回した後に修正コミットを積み、関門の印を押し直した。
  #835 では **AX の findings を直したコミット a70abd3e 自身が未レビュー** のまま
  帳簿上「レビュー済み」。しかも協働セッションの実測では **修正コミットこそ欠陥を
  作る** (1 回目の修正が 2 周目 findings 3 件のうち 2 件を作り、2 回目の修正の後に
  3 周目がさらに 1 件を拾い、収束していない)。つまりこの穴は **統計的にいちばん
  危ない領域を帳簿から隠していた**。

なぜ導出に替えたか
------------------
状態遷移の形を保ったまま「push で復活させる」経路を足しても、復活経路を 1 つ忘れれば
同じ穴が残る (1 経路だけ塞いで構造を閉じたと名乗る型)。導出なら忘れる経路が
構造的に無い:

    owed = registry の pr-open 必須種別
         − 「いまの head sha に対する実施記録」
         − 「理由付きの免除 (review-skips.jsonl)」

head を確かめられなければ **関門は閉じたまま** (2026-11-05 user 決定)。
「現在の差分に対して実施済み」と言えない状態で通すと偽の緑になり、緑のガードは
信頼されるので無ガードより悪い。
"""

import json

import pytest

import commands
import commands_shared as cs
import review_spine as rs

HEAD = "1111111111111111111111111111111111111111"
NEXT = "2222222222222222222222222222222222222222"


# ---------------------------------------------------------------------------
# 1. 純粋な導出規則 (review_spine.derive_pr_review_obligations)
# ---------------------------------------------------------------------------

def _derive(performed=(), skipped=(), head=HEAD, required=("ax", "maintainability")):
    return rs.derive_pr_review_obligations(
        required=list(required), performed=list(performed),
        skipped=list(skipped), head_sha=head)


def test_a_review_pinned_to_this_head_is_credited():
    ob = _derive([{"review_type": "ax", "reviewed_sha": HEAD, "sha_pinned": True}])
    assert ob["satisfied"] == ["ax"]
    assert ob["owed"] == ["maintainability"]


def test_a_review_pinned_to_another_sha_is_owed_again():
    """**この課題の本体**: 実施後にコミットが積まれたら、その種別は要る側に戻る。"""
    ob = _derive([{"review_type": "ax", "reviewed_sha": HEAD, "sha_pinned": True}],
                 head=NEXT)
    assert ob["satisfied"] == []
    assert ob["owed"] == ["ax", "maintainability"]
    # 「一度やったのにまた要る」理由を、どの sha に対する実施だったかで言えること
    assert ob["stale"] == [{"review": "ax", "reviewed_sha": HEAD}]


def test_a_legacy_record_without_the_field_is_still_credited_but_disclosed():
    """この変更より前に書かれた記録は従来どおり差し引く (黙って厳しくしない)。

    ``LEGACY_REVIEW_DUE_TOLERANCE`` と同じ判断。既存の記録を孤児にすると、実際に
    実施したレビューがまとめて未実施に戻り、運用が止まる。ただし **差し引いたことと
    「現在の差分に対して」と言えないことを開示する**。
    """
    ob = _derive([{"review_type": "ax"}], head=NEXT)   # sha_pinned キーが無い
    assert ob["owed"] == ["maintainability"]
    assert ob["legacy_no_sha"] == ["ax"]
    assert ob["satisfied"] == [], "旧記録を『この head に対して実施』と偽っている"


def test_a_new_record_that_failed_to_pin_is_NOT_credited():
    """旧形式への寛容が **恒久的な抜け道** にならないこと。

    ここを legacy と同じ扱いにすると、gh が落ちている / offline で
    ``review done`` を打つたびに「sha 無しだから差し引く」記録が増え、以後の
    全差分を覆ってしまう (素朴に緩めると言い換えで素通りする型)。
    """
    ob = _derive([{"review_type": "ax", "reviewed_sha": "", "sha_pinned": False}],
                 head=HEAD)
    assert ob["owed"] == ["ax", "maintainability"]
    assert [u["review"] for u in ob["unpinned"]] == ["ax"]


def test_an_unpinned_record_is_not_credited_EVEN_IF_its_sha_matches_head():
    """``sha_pinned: False`` を名乗る記録は、sha が head と一致していても信用しない。

    **反転ハーネスが捕まえた偽の緑** (2026-11-05): 上の
    ``test_a_new_record_that_failed_to_pin_is_NOT_credited`` は sha 空の記録しか
    見ていなかったので、``sha_pinned is False`` の枝を丸ごと消しても
    「sha が空」の枝が代わりに拾って緑のままだった = その枝を守っていなかった。

    枝が本当に効くのは **sha を持ちながら pin していないと名乗る記録** のとき。
    いまの書き口はこの組み合わせを作らない (``sha_pinned = bool(reviewed_sha)``) が、
    台帳はローカルのファイルなので手書き / 破損で在りうる。名乗りと中身が食い違う
    記録は信用しない側に倒す。
    """
    ob = _derive([{"review_type": "ax", "reviewed_sha": HEAD, "sha_pinned": False}],
                 head=HEAD)
    assert ob["satisfied"] == [], "pin していないと名乗る記録を差し引いた"
    assert "ax" in ob["owed"]
    assert [u["review"] for u in ob["unpinned"]] == ["ax"]


def test_a_record_claiming_to_be_pinned_with_an_empty_sha_is_not_trusted():
    """名乗りと中身が食い違う記録は信用しない側に倒す。"""
    ob = _derive([{"review_type": "ax", "reviewed_sha": "", "sha_pinned": True}])
    assert "ax" in ob["owed"]
    assert [u["review"] for u in ob["unpinned"]] == ["ax"]


def test_an_unknown_head_credits_nothing_pinned():
    """head が不明なら、pin 済みの記録も「確かめられない」側に置く。"""
    ob = _derive([{"review_type": "ax", "reviewed_sha": HEAD, "sha_pinned": True}],
                 head="")
    assert ob["head_known"] is False
    assert ob["satisfied"] == []
    assert ob["owed"] == ["ax", "maintainability"]


def test_a_reason_bearing_skip_is_credited_and_discloses_when_it_was_made():
    """免除は sha 単位にしない (人間の判断を push ごとに出し直させない) が、
    **いつの時点の判断か** を持ち回ること。覆う範囲が広い判断ほど、いつのものか
    読めないと監査できない。"""
    ob = _derive(skipped=[{"review_type": "ax", "skipped_at_sha": HEAD}], head=NEXT)
    assert ob["owed"] == ["maintainability"]
    assert ob["skipped"] == [{"review": "ax", "skipped_at_sha": HEAD}]


def test_the_required_set_comes_from_the_registry_not_a_literal():
    """必須種別は registry 由来 (= 新しい pr-open 種別が自動で関門に入る)。

    ``scripts/review-gate-ci.py`` の required check と同じ集合を指すので、
    ローカルの関門と CI が別のことを言い始めない。
    """
    import importlib.util
    import os
    spec = importlib.util.spec_from_file_location(
        "_gate_ci", os.path.join(os.path.dirname(__file__), "..",
                                 "scripts", "review-gate-ci.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert (sorted(rs.required_pr_open_review_types())
            == sorted(mod.required_review_types())), (
        "関門と CI の required check が別の集合を見ている")


# ---------------------------------------------------------------------------
# 2. 免除ログの読み口が書き口と同じ綴りを読むこと
# ---------------------------------------------------------------------------

def test_the_skip_reader_reads_what_the_skip_writer_writes(tmp_path, monkeypatch):
    """書き手 (``build_review_skip_record``) と読み手 (``read_pr_review_skips``) が
    同じ綴りを使っていること。

    監査ログの PR 同定は ``ref_kind``/``ref``/``repo``。台帳側の ``pr_repo`` とは
    綴りが違うので、読み手が台帳の綴りで読むと **免除したのに関門が開かない**
    (しかも理由が表示されない) 状態になる。
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "project.json").write_text(
        json.dumps({"name": "t", "milestones": []}), encoding="utf-8")
    rec = rs.build_review_skip_record(
        review_type="ax", ref_kind="pr", ref="42", reason="docs のみ",
        actor="human", gate={"signal": "human-session"}, at="2026-11-05T00:00:00Z")
    rec["repo"] = "o/r"
    rec["skipped_at_sha"] = HEAD
    with open(tmp_path / ".beacon" / "review-skips.jsonl", "w",
              encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    got = cs.read_pr_review_skips("42", "o/r")
    assert [g["review_type"] for g in got] == ["ax"], got
    assert got[0]["skipped_at_sha"] == HEAD
    # 別リポジトリの同番号には効かない
    assert cs.read_pr_review_skips("42", "other/app") == []


def test_a_corrupt_skip_line_does_not_erase_the_rest(tmp_path, monkeypatch):
    """1 行の破損で免除の履歴全体を落とさないこと (= 理由の分からない赤を作らない)。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "project.json").write_text(
        json.dumps({"name": "t", "milestones": []}), encoding="utf-8")
    with open(tmp_path / ".beacon" / "review-skips.jsonl", "w",
              encoding="utf-8") as f:
        f.write("{ not json\n")
        f.write(json.dumps({"review_type": "ax", "ref_kind": "pr", "ref": "42",
                            "repo": "o/r"}, ensure_ascii=False) + "\n")
    assert [g["review_type"] for g in cs.read_pr_review_skips("42", "o/r")] == ["ax"]


# ---------------------------------------------------------------------------
# 3. head の問い合わせが試験中に生きた GitHub を叩かないこと
# ---------------------------------------------------------------------------

def test_resolve_pr_head_does_not_touch_live_github_under_test(monkeypatch):
    """既定の adapter のままなら gh を叩かず「確かめられなかった」を返すこと。

    関門がこの問いを毎回立てるようになったので、素通しだと試験全体が非 hermetic に
    なる (実測: 架空リポジトリ ``o/r`` で本物の GraphQL エラーが返った)。
    **本番の判定を曲げるのではなく** 「不明」を返すので、方針どおり関門は閉じた
    ままになり、偽の緑は作らない。
    """
    monkeypatch.setenv("BEACON_TEST_MODE", "1")
    sha, _state, err = cs.resolve_pr_head("42", "o/r")
    assert sha == "" and "試験中" in err


def test_a_stubbed_adapter_is_not_blocked_by_that_guard(monkeypatch):
    """guard が、意図して差し替えた stub を無効化しないこと。

    ここが効かないと、関門を開ける試験が書けなくなる (= この課題の試験自体が
    書けない)。
    """
    import _gh_head_stub
    _gh_head_stub.install(monkeypatch, {"42": HEAD})
    sha, state, err = cs.resolve_pr_head("42", "o/r")
    assert (sha, state, err) == (HEAD, "OPEN", "")


# ---------------------------------------------------------------------------
# 4. 書き口 — 台帳レコードが sha を持つこと
# ---------------------------------------------------------------------------

def test_the_ledger_record_carries_the_reviewed_sha(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "_review_done_dir",
                        lambda: (str(tmp_path / "ledger"), True))
    state = cs.record_review_done_across_worktrees(
        "ax", "42", "o/r", reviewed_sha=HEAD)
    assert state == cs.RECORDED_SHARED
    rec = cs.review_done_provenance("42")[0]
    assert rec["reviewed_sha"] == HEAD
    assert rec["sha_pinned"] is True


def test_the_ledger_record_marks_an_unpinned_write_as_such(tmp_path, monkeypatch):
    """sha を渡せなかった書き込みは ``sha_pinned: False`` と名乗ること。

    ここが ``False`` でないと、旧形式 (キー無し) と見分けが付かず、寛容が
    恒久的な抜け道になる。
    """
    monkeypatch.setattr(cs, "_review_done_dir",
                        lambda: (str(tmp_path / "ledger"), True))
    cs.record_review_done_across_worktrees("ax", "42", "o/r", reviewed_sha="")
    rec = cs.review_done_provenance("42")[0]
    assert rec["sha_pinned"] is False


# ---------------------------------------------------------------------------
# 5. 開示 — 緑の意味を弱めるものは必ず 1 行出す
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bucket,needle", [
    ("legacy_no_sha", "意味しません"),
    ("unpinned", "差し引きません"),
    ("skipped", "sha 単位ではない"),
    ("stale", "もう一度要ります"),
])
def test_every_weakening_bucket_is_disclosed(bucket, needle):
    """開示を 1 つでも落とすと、関門の緑が黙って意味を変える。

    バケツごとに 1 件入れて、その行が出ることを **全件** 確かめる (代表 1 つだけ
    測ると、残りを消しても緑になる)。
    """
    ob = {"head_sha": NEXT, "provenance": [], "satisfied": [],
          "legacy_no_sha": [], "unpinned": [], "stale": [], "skipped": []}
    if bucket == "legacy_no_sha":
        ob["legacy_no_sha"] = ["ax"]
    elif bucket == "unpinned":
        ob["unpinned"] = [{"review": "ax", "reason": "pin できていません"}]
    elif bucket == "skipped":
        ob["skipped"] = [{"review": "ax", "skipped_at_sha": HEAD}]
    else:
        ob["stale"] = [{"review": "ax", "reviewed_sha": HEAD}]
    lines = "\n".join(cs.format_pr_review_obligation_disclosure(ob))
    assert needle in lines, f"{bucket} を開示していない:\n{lines}"


def test_a_satisfied_type_discloses_which_sha_and_whose_record():
    """差し引いた根拠 (どの sha / 誰の記録) を言うこと。

    台帳はローカルのファイルなので手で書けば偽装できる。防げるのは
    **黙って関門が外れないようにする** ことだけ (ms-166 e-6899 と同じ判断)。
    """
    ob = {"head_sha": HEAD, "satisfied": ["ax"],
          "legacy_no_sha": [], "unpinned": [], "stale": [], "skipped": [],
          "provenance": [{"review_type": "ax", "reviewed_sha": HEAD,
                          "recorded_by": "someone", "recorded_at": "2026-11-05"}]}
    line = "\n".join(cs.format_pr_review_obligation_disclosure(ob))
    assert HEAD[:12] in line and "someone" in line, line
