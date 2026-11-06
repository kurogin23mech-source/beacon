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


def _gate_ci_module():
    import importlib.util
    import os
    spec = importlib.util.spec_from_file_location(
        "_gate_ci", os.path.join(os.path.dirname(__file__), "..",
                                 "scripts", "review-gate-ci.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_required_set_comes_from_the_registry_not_a_literal(monkeypatch):
    """必須種別は registry 由来 (= 新しい pr-open 種別が自動で関門に入る)。

    **substring では測らない** (2026-11-06 に反転ハーネスが捕まえた): 初版は
    ``inspect.getsource`` に ``batch_review_types_for_node`` が含まれるかを見て
    いたが、**docstring に同じ語が書いてある** ので、本体を ``return ["ax"]`` に
    書き換えても緑のままだった = 偽の安全。緩い一致はこのリポジトリが繰り返し
    踏んでいる形なので、registry を差し替えて出力が追従するかを振る舞いで測る。
    """
    assert rs.required_pr_open_review_types(), "必須種別が空"
    monkeypatch.setattr(
        rs, "batch_review_types_for_node",
        lambda node, registry=None: [{"review": "FROM-THE-REGISTRY",
                                      "judge_run": True}])
    assert rs.required_pr_open_review_types() == ["FROM-THE-REGISTRY"], (
        "registry から導出していない (リテラルを返している)")


def test_the_ci_required_check_delegates_instead_of_recomputing(monkeypatch):
    """CI の required check が **同じ 1 関数に委譲** していること。

    独立保守性レビュー 2 周目: 以前は CI 側が同じ式を独立に計算しており、
    一致は偶然だった (しかも関門側の docstring が「ズレない」と約束していた =
    何も強制しない約束)。片方で集合を絞ると、もう片方は別の集合を要求し続ける ——
    このPR自身の欠陥 (judge が見た差分と関門が守る差分がズレる) を必須種別の軸で
    再生産する形。

    **出力の一致では測れない** (委譲すれば自明に真になり、守る力を失う)。
    1 箇所を差し替えて、CI 側がそれに追従することを振る舞いで測る。
    """
    mod = _gate_ci_module()
    monkeypatch.setattr(rs, "required_pr_open_review_types",
                        lambda *a, **k: ["ONLY-FROM-THE-SINGLE-SOURCE"])
    assert mod.required_review_types() == ["ONLY-FROM-THE-SINGLE-SOURCE"], (
        "CI 側が独立に計算している — 片方を変えても追従しない")


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


# --- ドッグフーディングで自分が踏んだ名乗りのズレ (2026-11-06) ---------------

def test_review_done_asks_the_real_head_not_the_recorded_sha(tmp_path, monkeypatch):
    """``review done`` の「gate を通過できます」は **いまの head** に対する判定であること。

    **この機構を自分に当てて踏んだ** (2026-11-06)。1 周目のレビューを
    ``--reviewed-sha <古い sha>`` で正直に記録したところ、

        kurogin23mech-source/beacon#838 の独立レビューは全て 1ccc819d12d0 に対して
        実施済み — approve/merge の gate を通過できます

    と出たが、同じ瞬間に ``beacon review status`` は両種別を未実施 (exit 2) と
    答えた。名乗りと実態がズレていた —— この課題が消すために在る欠陥と同じ型を、
    直したばかりの場所で作っていた。

    記録した sha に対する充足は「記録の事実」であって「関門が開いたか」ではない。
    """
    import io
    from contextlib import redirect_stdout, redirect_stderr

    old_sha, cur_head = HEAD, NEXT
    tdir = _marker_env(tmp_path, monkeypatch)
    monkeypatch.setattr(commands, "_get_triggers_dir", lambda: str(tdir))
    monkeypatch.setattr(cs, "_review_done_dir",
                        lambda: (str(tmp_path / "ledger"), True))
    monkeypatch.setattr(commands, "_record_review_adjudication_decision",
                        lambda *a, **k: None)
    monkeypatch.setattr(commands, "_ci_flip_review_gate_success",
                        lambda *a, **k: None)
    # いまの head は cur_head (= 記録する sha とは別物)
    import _gh_head_stub
    _gh_head_stub.install(monkeypatch, {"838": cur_head})
    for t in rs.required_pr_open_review_types():
        (tdir / cs.review_due_filename(t, "838", "o/r")).write_text(
            json.dumps({"review": t, "pr_repo": "o/r"}), encoding="utf-8")

    out, err = io.StringIO(), io.StringIO()
    for t in rs.required_pr_open_review_types():
        monkeypatch.setenv("BEACON_REVIEW_TYPE", t)
        monkeypatch.setenv("BEACON_PR_NUMBER", "838")
        monkeypatch.setenv("BEACON_PR_REPO", "o/r")
        monkeypatch.setenv("BEACON_REVIEWED_SHA", old_sha)
        monkeypatch.setenv("BEACON_REVIEW_NO_FINDINGS", "1")
        try:
            with redirect_stdout(out), redirect_stderr(err):
                commands.cmd_review_done()
        except SystemExit:
            pass
    combined = out.getvalue() + err.getvalue()

    assert "gate を通過できます" not in combined, (
        "いまの head を見ていないのに通過を名乗った:\n" + combined)
    assert "関門はまだ開いていません" in combined, combined
    # どちらの sha の話かを両方言うこと (記録は残る / 現在の差分には未実施)
    assert old_sha[:12] in combined and cur_head[:12] in combined, combined


# ===========================================================================
# 7. 独立レビュー 2 周目 (PR #838) の指摘に対する修正
#
# 1 周目の修正コミット自身に 5 件出た (引き継ぎが言っていた「修正コミットこそ
# 欠陥を作る」の実測。3 周目にあたる)。どれも「新しく足した箇所が判定の一部を
# 写し忘れる」型で、このPRが主題にしている欠陥と同じ家族。
# ===========================================================================

def _ob(**kw):
    import _pr_obligations_stub
    return _pr_obligations_stub.obligations(**kw)


@pytest.mark.parametrize("state,resolved,owed,passed,why", [
    ("MERGED", True, ["ax"], True, "merge 済みに課す義務は無い"),
    ("CLOSED", True, ["ax"], True, "close 済みに課す義務は無い"),
    ("OPEN", False, [], False, "head 不明は止める側に倒す"),
    ("OPEN", True, ["ax"], False, "未実施が在れば止める"),
    ("OPEN", True, [], True, "全種別が今の head に対して実施済みなら通す"),
])
def test_the_pass_verdict_lives_in_one_place(state, resolved, owed, passed, why):
    """通す / 止めるの判定は ``passed`` 1 箇所 (ms-119 e-6859、AX 2 周目 high)。

    以前は呼び出し側が ``owed`` / ``resolved`` / ``pr_state`` を見て自分で
    再計算しており、新しく足した ``beacon review status`` が本物の関門と
    2 点で食い違った。
    """
    ob = {"pr_state": state, "resolved": resolved, "owed": list(owed)}
    assert cs._review_gate_passed(ob) is passed, why


def test_review_status_exits_the_same_way_in_both_output_modes(monkeypatch, capsys):
    """``--json`` とテキストで **終了コードが一致** すること。

    judge の実測: ``review status --pr 838 --json`` が owed 2 件で exit 0、同じ PR の
    テキストモードは exit 2。README には自分で「Exits 2 while anything is owed」と
    書いておきながら、テキストモードにしか実装していなかった。
    **同じ問いに、出力形式だけで違う答えが返る** 形で、「機械可読が欲しいから
    --json を付ける」という自然な選択が偽の緑を踏む。
    """
    ob = _ob(owed=rs.required_pr_open_review_types(), satisfied=[])
    monkeypatch.setattr(commands, "pr_review_obligations",
                        lambda *a, **k: ob)
    monkeypatch.setattr(commands, "_resolve_review_pr_repo", lambda *a, **k: "o/r")
    monkeypatch.setenv("BEACON_PR_NUMBER", "838")
    monkeypatch.setenv("BEACON_PR_REPO", "o/r")

    codes = {}
    for mode in ("text", "json"):
        monkeypatch.setenv("BEACON_JSON", "1" if mode == "json" else "")
        try:
            commands.cmd_review_status()
            codes[mode] = 0
        except SystemExit as e:
            codes[mode] = e.code if isinstance(e.code, int) else 0
        capsys.readouterr()
    assert codes["text"] == codes["json"] == 2, (
        "出力形式だけで終了コードが違う: " + str(codes))


def test_review_status_and_the_gate_agree_about_a_merged_pr(monkeypatch, capsys):
    """マージ済み PR について ``review status`` と関門が同じことを言うこと。

    judge の実測: 関門 (``_review_gate_check``) は MERGED を素通りさせるのに、
    ``review status`` は「未実施、approve / merge は止まります」と言い、
    /beacon-review-run の実行まで促した。同じ ``pr_review_obligations`` を土台に
    しているのに、早期 return が片方にしか写っていなかった。
    """
    import cmd_pr
    ob = _ob(owed=rs.required_pr_open_review_types(), satisfied=[],
             pr_state="MERGED")
    monkeypatch.setattr(commands, "pr_review_obligations", lambda *a, **k: ob)
    monkeypatch.setattr(cmd_pr, "pr_review_obligations", lambda *a, **k: ob)
    monkeypatch.setattr(commands, "_resolve_review_pr_repo", lambda *a, **k: "o/r")
    monkeypatch.setenv("BEACON_PR_NUMBER", "2")
    monkeypatch.setenv("BEACON_PR_REPO", "o/r")
    monkeypatch.setenv("BEACON_JSON", "")

    # 関門: 止めない
    cmd_pr._review_gate_check("2", action="merge", pr_repo="o/r")
    # status: 同じ判断 (exit 0) で、しかも理由を言う
    try:
        commands.cmd_review_status()
        code = 0
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 0
    out = capsys.readouterr().out
    assert code == 0, "関門は通すのに status は止めた"
    assert "MERGED" in out and "問いません" in out, out
    assert "/beacon-review-run" not in out, (
        "マージ済み PR にレビューの実行を促している:\n" + out)


def test_the_gate_reads_passed_instead_of_recomputing():
    """関門が ``owed`` / ``resolved`` / ``pr_state`` から判定を再計算しないこと。

    再計算が復活すると、次に足される 3 つ目の呼び出し元が同じ取り違えをする。
    """
    import cmd_pr
    import inspect
    src = inspect.getsource(cmd_pr._review_gate_check)
    assert 'ob.get("passed")' in src, "passed を読んでいない"
    assert 'pr_state") in ("MERGED"' not in src, (
        "関門が MERGED/CLOSED の判定を自前で持っている (判定の所有が 2 箇所)")


def test_the_report_decision_is_a_pure_function():
    """``review done`` の「どの行を出すか / どう終わるか」が純粋に決まること。"""
    ob = _ob(owed=["ax"], satisfied=[])
    r = cs.decide_review_done_report(
        ob, remaining=[], owed_before=["ax"], reviewed_sha=HEAD,
        already_recorded=False, where="o/r#838")
    assert r["exit_code"] is None
    assert r["stamp_marker"] is True
    joined = "\n".join(t for _s, t in r["lines"])
    assert "関門はまだ開いていません" in joined, joined

    # 元から義務が無かった = 非 0 で終える (冪等な打ち直しは 0)
    r2 = cs.decide_review_done_report(
        ob, remaining=[], owed_before=[], reviewed_sha=HEAD,
        already_recorded=False, where="o/r#838")
    assert r2["exit_code"] == 3
    r3 = cs.decide_review_done_report(
        ob, remaining=[], owed_before=[], reviewed_sha=HEAD,
        already_recorded=True, where="o/r#838")
    assert r3["exit_code"] is None


def test_the_test_helper_lives_in_one_place():
    """``_all_types_done_at`` の分岐が共有 stub に 1 箇所だけ在ること。

    独立保守性レビュー 2 周目: この helper が 2 つの試験ファイルに docstring ごと
    逐語で重複していた。どちらも「手書きすると偽の緑になる」という理由で共有
    factory に委譲していたのに、**どの head_sha なら充足を返すかを決める分岐** は
    委譲せず手書きのままだった = 対策コードの隣に同じ失敗モードを作っていた。
    """
    import _pr_obligations_stub
    assert hasattr(_pr_obligations_stub, "install_satisfied_at")
    import inspect
    for mod_name in ("test_review_gate_ci", "test_review_gate_stamp_visibility_e6873"):
        mod = __import__(mod_name)
        src = inspect.getsource(mod._all_types_done_at)
        assert "install_satisfied_at" in src, (
            f"{mod_name} が分岐を手書きしている")
        assert "def fake(" not in src, (
            f"{mod_name} に fake の分岐が残っている")
