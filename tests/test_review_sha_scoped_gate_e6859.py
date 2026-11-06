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


# ===========================================================================
# 6. 独立レビュー (AX / 保守性, PR #838) の指摘に対する修正
#
# judge 2 体が挙げた 7 件を全て直した。ここはその各点を留める。どれも
# 「直したつもり」で終わらせないために、**壊れる側の形** を 1 件ずつ測る。
# ===========================================================================

# --- AX① 促しと関門が同じ意味になったか -----------------------------------
#
# judge の実測した事故の筋道: 古い head に `review done` した後、コミットを積んで
# `beacon trigger check` を叩くと促しが 1 件も出ない。関門 (approve/merge) は
# 正しく止まるが、`BEACON_REVIEW_GATE_CI` 未設定のリポジトリでは `gh pr merge` /
# UI のマージボタンが関門を経由しないので、促しの空リストを信じて直接マージ経路へ
# 進むと、この課題が塞いだはずの事故が **読み取り専用の確認経路側から** 再発する。

def _marker_env(tmp_path, monkeypatch):
    tdir = tmp_path / "triggers"
    tdir.mkdir(exist_ok=True)
    monkeypatch.setattr(cs, "_get_triggers_dir", lambda: str(tdir))
    return tdir


def test_the_nudge_marker_records_which_diff_was_reviewed(tmp_path, monkeypatch):
    _marker_env(tmp_path, monkeypatch)
    cs.write_reviewed_marker("838", "o/r", HEAD)
    assert cs.reviewed_marker_sha("838", "o/r") == HEAD


def test_the_nudge_refires_after_a_commit_is_pushed(tmp_path, monkeypatch):
    """**AX① の本体**: 実施後にコミットを積んだら促しが戻ること。"""
    import cmd_trigger
    _marker_env(tmp_path, monkeypatch)
    cs.write_reviewed_marker("838", "o/r", HEAD)
    assert cmd_trigger._reviewed_marker_covers_head("838", "o/r", HEAD) is True, (
        "見た差分のままなら促しを抑止してよい")
    assert cmd_trigger._reviewed_marker_covers_head("838", "o/r", NEXT) is False, (
        "コミットを積んだのに促しを抑止している (= 促しが消えたままになる)")


@pytest.mark.parametrize("marker_sha,head,why", [
    (None, HEAD, "マーカーが無い (まだ一度も実施していない)"),
    (HEAD, "", "head が取れない (一致を確かめられない)"),
])
def test_the_nudge_errs_toward_firing(tmp_path, monkeypatch, marker_sha, head, why):
    """促しは **出す側に倒す**。

    関門は止める側へ倒すが、促しは止める力を持たないので非対称が正しい ——
    過剰に促して煩いのは実害が小さく、促さないのは「もう要らない」と誤読させて
    事故に直結する。
    """
    import cmd_trigger
    _marker_env(tmp_path, monkeypatch)
    if marker_sha:
        cs.write_reviewed_marker("838", "o/r", marker_sha)
    assert cmd_trigger._reviewed_marker_covers_head("838", "o/r", head) is False, why


def test_a_legacy_marker_without_a_sha_does_not_suppress_the_nudge(
        tmp_path, monkeypatch):
    """旧形式のマーカー (レビュー種別の文字列だけ) は抑止に使えないこと。

    「どの差分まで見たか」を言っていないので、抑止の根拠にならない。
    """
    import cmd_trigger
    tdir = _marker_env(tmp_path, monkeypatch)
    with open(cs._pr_open_reviewed_marker_path("838", "o/r"), "w",
              encoding="utf-8") as f:
        f.write("ax")                      # 旧形式
    assert cs.reviewed_marker_sha("838", "o/r") == ""
    assert cmd_trigger._reviewed_marker_covers_head("838", "o/r", HEAD) is False


def test_the_anchor_asks_github_for_the_head(tmp_path, monkeypatch):
    """促しの再発火が **いまの head を見ている** こと (= gh に聞いていること)。

    `headRefOid` を聞かなくなると、促しは再び head を見られなくなる (そして
    判定は「抑止しない」側に倒れるので、黙って過剰発火に変わる = 気づきにくい)。
    """
    import inspect
    import cmd_trigger
    src = inspect.getsource(
        cmd_trigger._auto_fire_pr_open_review_triggers_for_open_prs)
    assert "headRefOid" in src, "gh に head を聞いていない"
    assert "_reviewed_marker_covers_head" in src, "head を見た抑止判定を通っていない"


# --- AX② 「どの差分を見たか」の履歴が残るか --------------------------------

def test_the_reviewed_sha_survives_in_the_append_only_decision_arm(monkeypatch):
    """台帳は上書きで履歴を持たないので、**追記のみの判断記録** に sha を残すこと。

    judge の指摘: この課題の核心価値は「どの差分を見たかを辿れるようにする」
    なのに、その事実を持つ唯一の置き場が (PR, 種別) ごとに上書きされるため、
    次の実施で前の sha が消える。PR #785 の経緯 (1 周目は古い sha のみ実施、
    保守性だけ後から再実施) を、まさにそれを直した後の仕組みで再現できない。
    """
    captured = {}

    class _Client:
        def record_decision(self, project_id, payload):
            captured.update(payload)

    import contextlib
    monkeypatch.setattr(cs, "_is_cloud_mode", lambda: True)
    # ``_get_api_client`` は ``(client, config)`` を返し、呼び出し側は
    # ``config["project_id"]`` を読む (空なら何も書かずに戻る)。
    monkeypatch.setattr(cs, "_get_api_client",
                        lambda: (_Client(), {"project_id": "p"}))
    monkeypatch.setattr(cs, "best_effort_decision_write",
                        lambda *a, **k: contextlib.nullcontext())
    commands._record_review_adjudication_decision(
        "ax", "838", "採用 1 / 却下 0", [], "o/r", no_findings=False,
        reviewed_sha=HEAD)
    assert captured, "判断記録が書かれていない"
    assert f"sha:{HEAD}" in captured.get("evidence", []), (
        "どの差分に対する実施かが追記記録に残っていない: "
        + str(captured.get("evidence")))


# --- AX③ 開示のバケツが排他か ----------------------------------------------

@pytest.mark.parametrize("performed,skipped,winner", [
    ([{"review_type": "ax", "reviewed_sha": HEAD, "sha_pinned": True}],
     [{"review_type": "ax", "skipped_at_sha": HEAD}], "satisfied"),
    ([{"review_type": "ax"}],
     [{"review_type": "ax", "skipped_at_sha": HEAD}], "legacy_no_sha"),
    ([], [{"review_type": "ax", "skipped_at_sha": HEAD}], "skipped"),
])
def test_one_type_is_disclosed_in_exactly_one_bucket(performed, skipped, winner):
    """同一種別が 2 つのバケツに載らないこと (強い根拠が勝つ)。

    judge が実測した形: 実施と免除の両方の記録が在ると、開示に
    ``✓ ax: <sha> に対して実施`` と ``⊘ ax: 人間が理由付きで免除`` が
    **同時に出た**。「差し引いた根拠を必ず言う」という設計意図が、2 つの矛盾する
    根拠を並べることで「どちらを信じればいいか分からない」形に転化していた。
    """
    ob = _derive(performed, skipped, head=HEAD)
    buckets = {
        "satisfied": list(ob["satisfied"]),
        "legacy_no_sha": list(ob["legacy_no_sha"]),
        "skipped": [s["review"] for s in ob["skipped"]],
    }
    hit = [name for name, types in buckets.items() if "ax" in types]
    assert hit == [winner], f"バケツが {hit} (期待: [{winner}]) — {buckets}"
    lines = cs.format_pr_review_obligation_disclosure(
        {**ob, "provenance": [{"review_type": "ax", "reviewed_sha": HEAD}]})
    ax_lines = [l for l in lines if " ax:" in l]
    assert len(ax_lines) == 1, "開示が 1 種別について 2 行以上出ている:\n" + "\n".join(ax_lines)


# --- AX④ sha の形を検査するか ----------------------------------------------

@pytest.mark.parametrize("value,ok", [
    ("1ccc819d12d06f043ed51483b30539ab9aef5177", True),
    ("1ccc819", True),
    ("a3f91", False),          # 桁が足りない (貼り間違い)
    ("not-a-real-sha!!", False),
    ("ZZZZZZZ", False),
    ("", False),
])
def test_the_sha_shape_is_checked(value, ok):
    """judge の実測: ``--reviewed-sha "not-a-real-sha!!"`` が通り、即座に
    「この sha に対する実施として記録しました」と **成功を名乗った**。
    次に head を解決できれば stale に戻るので実害は限定的だが、その場の成功文言が
    誤った確信を生むのが欠陥。
    """
    assert cs.is_commit_sha_shaped(value) is ok


# --- 保守性① 文面の組み立てが判定から切り出されているか ---------------------

def test_the_gate_messages_are_pure_builders():
    """文面が純粋関数として単独で呼べること (= 判定と混ざっていないこと)。

    judge の指摘: 切り出す前は ``_review_gate_check`` が 32 → 109 行に膨らみ、
    分岐と文面生成が同じ行に混在していた。「ある分岐の文面だけ直す」つもりで
    判定条件を壊す事故が起きやすい形。姉妹の
    ``format_pr_review_obligation_disclosure`` と同じ手筋に揃えた。
    """
    ob = _derive([], [], head="")
    ob["head_error"] = "gh が落ちている"
    msg = cs.format_review_head_unresolved_error(
        ob, action="merge", pr_number="838", pr_repo="o/r")
    assert "merge できません" in msg and "gh が落ちている" in msg
    assert "gh pr view 838 --repo o/r" in msg, "復帰手順にリポジトリが入っていない"

    ob2 = _derive([{"review_type": "ax", "reviewed_sha": HEAD, "sha_pinned": True}],
                  [], head=NEXT)
    blocked = cs.format_review_gate_block_error(ob2, action="approve",
                                                pr_number="838")
    assert "積まれた" in blocked, "一度やったのに赤い理由を言っていない"
    assert HEAD[:12] in blocked, "どの sha に対する実施だったかを言っていない"
    # 副作用なしで確かめる経路を案内すること (AX① で足した動詞)
    assert "beacon review status" in blocked


# --- 保守性② 試験の fake が本物の形から導出されているか ---------------------

def test_the_shared_obligations_fake_is_derived_from_the_real_function():
    """fake の形を手書きしないこと。

    judge が反転実験で実測: ``owed`` キー名を変えると本物の経路を通る試験は
    赤くなるのに、手書き fake を使う試験群は無傷で緑だった。fake 経由の試験に
    「本物の形が変わった」を検知する経路が無かった = このPRが主題にしている
    偽の緑と同じ形の弱点を、このPRが新設した試験の中に残していた。
    """
    import _pr_obligations_stub as stub

    # 既定は「関門が開く」形 (関門を主題にしない試験が主題まで到達するため)
    fake = stub.obligations()
    assert fake["owed"] == []
    assert fake["satisfied"] == rs.required_pr_open_review_types()

    # **振る舞いで測る**: 本物の返り値に手を入れたら、factory の出力にもそれが
    # 現れること。キー集合の比較では「本物と同じ形を手書きした fake」と区別できず、
    # 反転ハーネスが実際に素通りした (= この試験自身が偽の緑だった、2026-11-06)。
    # 導出しているかは「本物を通っているか」でしか測れない。
    import review_spine
    _real = review_spine.derive_pr_review_obligations
    _seen = {}

    def _spy(**kwargs):
        _seen.update(kwargs)
        out = _real(**kwargs)
        out["_DERIVED_FROM_THE_REAL_FUNCTION"] = True
        return out

    review_spine.derive_pr_review_obligations = _spy
    try:
        spied = stub.obligations(head_sha=HEAD)
    finally:
        review_spine.derive_pr_review_obligations = _real

    assert spied.get("_DERIVED_FROM_THE_REAL_FUNCTION") is True, (
        "factory が本物の純粋関数を通っていない (形を手書きしている) — "
        "本物のキーが変わっても古い形を返し続け、fake 経由の試験群が偽の緑になる")
    assert _seen.get("head_sha") == HEAD, (
        "factory が head_sha を本物に渡していない: " + str(_seen))


# --- 保守性③ 促しの面が名前で分かるか --------------------------------------

def test_the_nudge_face_is_named_as_such():
    """関門と促しが名前で区別できること。

    judge の指摘: 旧名 ``_pending_review_types_for_pr`` はどちらの面か言わないので、
    文脈ゼロの AI が「PR のレビュー義務」を grep するとこれが真っ先に見つかり、
    既存試験が大量に直接呼んでいるため「これが正しい API」と誤認する。この課題の
    主題が「お願いではなく構造で閉じる」なのに、この 1 関数の誤用防止だけ長い
    docstring (= お願い) に依存していた。
    """
    assert hasattr(cs, "_pr_open_review_nudge_pending_types")
    assert not hasattr(cs, "_pending_review_types_for_pr"), (
        "どちらの面か言わない旧名が残っている")
    import cmd_pr
    import inspect
    gate = inspect.getsource(cmd_pr._review_gate_check)
    assert "nudge" not in gate, "関門が促しの面を読んでいる (e-6859 の退行)"
