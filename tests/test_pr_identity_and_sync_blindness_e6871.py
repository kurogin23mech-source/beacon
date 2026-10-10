"""PR の識別と突合の盲点を塞いだことの試験 (ms-166 e-6871)。

何が起きていたか
----------------
`beacon pr add 772` のように **裸の番号** で登録すると、`gh pr view` は番号でも URL でも
受けるのでタイトルまで取れて成功する。だが beacon 側は

* `meta.url` に `'772'` をそのまま保存し、
* `meta.pr_number` は URL の形を要求する正規表現で取るので `None` になる

という状態になった。PR を指す経路が 2 つ (`pr_number` と `url`) あるのは冗長化の意図
だったが、**両方が同じ未解析の入力から導かれていた**ので実は冗長ではなかった。

その結果 `plan_pr_sync` がこの記録を識別できず `continue` で無言に飛ばし、飛ばした件数も
報告しないため、`beacon pr sync --dry-run` が

    All beacon PR entries are already aligned with GitHub.

と **嘘の「異常なし」** を返した。実測 2026-10-06 の時点で e-6725 / e-6751 は GitHub 上
MERGED の PR を `in_review` のまま持っていた。何も言わないより悪い — 読み手は緑を
信じて次に進む。

副作用として `beacon pr add` が「同じマイルストーンに並列で open な PR が 2 件ある」と
存在しない競合を警告し、`beacon claim` での調整を促した (実際は両方マージ済)。

この file が留めるもの
----------------------
1. 識別が 1 箇所 (core.identify_pr) に在り、URL と裸の番号の両方を受けること
2. 突合が、識別できない記録を **報告する** こと (無言で飛ばさない)
3. 整合を名乗る文言が、見ていない記録があるときに出ないこと
4. 裸の番号で登録された記録が、突合で拾われて GitHub の状態に揃うこと
5. 同じ PR を指す記録が複数あれば報告されること
"""

from __future__ import annotations

import io
import os
import re
import sys
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import cmd_pr  # noqa: E402
import core  # noqa: E402


# --- 1. 識別の単一真実源 -----------------------------------------------------

def test_identify_pr_takes_a_url_and_a_bare_number():
    assert core.identify_pr("https://github.com/o/r/pull/12") == (
        12, "https://github.com/o/r/pull/12")
    # 裸の番号は番号だけ分かる。どのリポジトリかは分からないので URL は None
    # (ここでリポジトリ名を捏造しないことが大事 — 呼び出し側が gh に聞く)。
    assert core.identify_pr("772") == (772, None)


def test_canonical_url_is_normalised_to_the_minimal_form():
    """末尾付きの URL を最小形に揃えること (独立 AX レビュー AX-4)。

    canonical という名前は「同じ PR なら同じ文字列になる」ことを約束している。
    入力をそのまま返すと ".../pull/12/files" と ".../pull/12" が別の文字列として
    canonical を名乗り、将来この値を文字列比較で同一性判定に使う人が約束を信じて
    誤判定する (今の重複検出は整数の番号で行っているので実害は出ていない)。

    最初に書いた試験は ".../pull/12" (= 既に最小形) しか渡しておらず、**正規化を
    外しても緑のまま**だった (mutation テストで気づいた)。末尾の付いた形を渡す。
    """
    base = "https://github.com/o/r/pull/12"
    for suffix in ("/files", "/commits", "?diff=split", "#issuecomment-1", "/"):
        n, url = core.identify_pr(base + suffix)
        assert (n, url) == (12, base), (suffix, n, url)


def test_identify_pr_refuses_what_it_cannot_identify():
    for junk in ("", "abc", "0", "-3", "not-a-pr",
                 "https://github.com/o/r/pull/12x"):
        assert core.identify_pr(junk) == (None, None), junk


def test_pr_add_does_not_keep_its_own_pr_number_regex():
    """識別の写しを増やさない。

    core.pr_add は自前の r'/pull/(\\d+)' を持っており、canonical 側
    (r"/pull/(\\d+)(?:[/?#].*)?$") より緩いので端の形で食い違った
    (".../pull/12x" から旧実装は 12 を取り、canonical は取らない)。
    """
    src = open(os.path.join(os.path.dirname(__file__), "..", "lib", "core.py"),
               encoding="utf-8").read()
    start = src.index("def pr_add(")
    body = src[start:src.index("\ndef ", start + 10)]
    # 散文のコメントではなく **実際の抽出呼び出し** を見る (コメントで "/pull/" に
    # 触れただけで落ちる検査は、緩い一致の逆 = 厳しすぎて偽陽性になる)。
    assert not re.search(r"_?re\.(search|match|findall)\(\s*r?['\"][^'\"]*/pull/", body), (
        "pr_add が PR 番号の抽出を自前で持っている。identify_pr に寄せること:\n" + body)
    assert "identify_pr(" in body, "pr_add が identify_pr を通っていない"


# --- 2 & 3. 突合は飛ばしたものを報告する ------------------------------------

def _entry(eid, url, pr_number=None, status="in_review"):
    return {"id": eid, "type": "pr", "status": status,
            "meta": {"url": url, "pr_number": pr_number}}


def _data(*entries):
    return {"milestones": [{"id": "ms-1", "entries": list(entries)}]}


def test_an_unidentifiable_entry_is_reported_not_silently_skipped():
    data = _data(_entry("e-5", "not-a-pr"))
    actions = core.plan_pr_sync(data, [{"number": 9, "state": "OPEN"}])
    kinds = {a["action"] for a in actions}
    assert "unreadable" in kinds, (
        "どの PR か判別できない記録を無言で飛ばしている: " + repr(actions))
    bad = [a for a in actions if a["action"] == "unreadable"]
    assert bad[0]["entry_id"] == "e-5"
    assert "not-a-pr" in bad[0]["reason"], "理由にその値を含めていない"


def test_an_entry_github_does_not_know_is_reported_not_silently_skipped():
    """照合できていないことと、整合していることは別。"""
    data = _data(_entry("e-6", "https://github.com/o/r/pull/999"))
    actions = core.plan_pr_sync(data, [{"number": 9, "state": "OPEN"}])
    assert "unmatched" in {a["action"] for a in actions}, repr(actions)


def test_the_all_aligned_line_cannot_appear_while_something_was_not_looked_at():
    """「整合しています」を名乗れるのは、見ていないものが 0 件のときだけ。

    これが旧実装の病理そのもの: 状態遷移が 0 件なら、識別できなかった記録が
    何件あっても無条件に「すべて整合」と出していた。
    """
    data = _data(_entry("e-5", "not-a-pr"))
    actions = core.plan_pr_sync(data, [{"number": 9, "state": "OPEN"}])
    buf = io.StringIO()
    with redirect_stdout(buf):
        cmd_pr._print_pr_sync_plan(actions)
    out = buf.getvalue()
    assert "整合しています" not in out, (
        "見ていない記録があるのに整合を名乗っている:\n" + out)
    assert "判別できない" in out and "e-5" in out, (
        "判別できなかった記録の件数と ID を出していない:\n" + out)


def test_the_all_aligned_line_does_appear_when_everything_was_checked():
    """逆側も留める。全部見て異常が無いときは、ちゃんとそう言えること
    (警告を出すだけの臆病なツールにしない)。"""
    data = _data(_entry("e-7", "https://github.com/o/r/pull/9", 9, status="in_review"))
    actions = core.plan_pr_sync(
        data, [{"number": 9, "state": "OPEN", "url": "https://github.com/o/r/pull/9"}])
    buf = io.StringIO()
    with redirect_stdout(buf):
        cmd_pr._print_pr_sync_plan(actions)
    out = buf.getvalue()
    assert "整合しています" in out, out
    assert "判別できなかった記録 0 件" in out, "見た件数を言っていない:\n" + out
    assert "照合した記録 1 件" in out, (
        "見た件数が実際と合っていない (OPEN な記録を数えていない):\n" + out)


# --- 4. 裸の番号で登録された記録が拾われて揃う -------------------------------

def test_a_bare_number_entry_is_picked_up_and_aligned():
    """実際に残っていた形 (url='772' / pr_number=None / in_review) が、
    GitHub の MERGED に揃い、識別の形も canonical になること。"""
    data = _data(_entry("e-6725", "772"))
    gh = [{"number": 772, "state": "MERGED",
           "url": "https://github.com/o/r/pull/772"}]
    actions = core.plan_pr_sync(data, gh)
    assert any(a["action"] == "merge" and a["entry_id"] == "e-6725"
               for a in actions), repr(actions)
    summary = core.apply_pr_sync(data, actions)
    meta = data["milestones"][0]["entries"][0]["meta"]
    assert meta["pr_number"] == 772, meta
    # URL は gh が言った値に揃える (リポジトリ名を推測しない)
    assert meta["url"] == "https://github.com/o/r/pull/772", meta
    assert summary["repaired"] >= 1, summary
    assert data["milestones"][0]["entries"][0]["status"] == "done"


def test_repair_does_not_invent_a_repository_when_github_is_silent():
    """gh が URL を教えてくれないときは URL を変えない。

    整数キーだけ埋めれば後段は識別できる。それ以上の推測 (リポジトリ名の捏造) は
    しない — 嘘の URL は、読んだ人をよそのリポジトリへ連れて行く。
    """
    data = _data(_entry("e-1", "772"))
    gh = [{"number": 772, "state": "MERGED"}]  # url を持たない行
    core.apply_pr_sync(data, core.plan_pr_sync(data, gh))
    meta = data["milestones"][0]["entries"][0]["meta"]
    assert meta["pr_number"] == 772
    assert meta["url"] == "772", "URL を捏造している: " + repr(meta)


# --- 5. 二重登録の検出 -------------------------------------------------------

def test_two_entries_pointing_at_the_same_pr_are_reported():
    """実測で残っていた形: e-6748 (done) と e-6751 (in_review) が両方 PR 775。
    登録時に既存を検出できていないので、片方が古い状態で残り続け、
    「並列で open な PR が 2 件」という誤報の種にもなる。"""
    data = _data(_entry("e-6748", "775", status="done"),
                 _entry("e-6751", "775", status="in_review"))
    actions = core.plan_pr_sync(
        data, [{"number": 775, "state": "MERGED",
                "url": "https://github.com/o/r/pull/775"}])
    dups = [a for a in actions if a["action"] == "duplicate"]
    assert dups, "同じ PR を指す 2 件を報告していない: " + repr(actions)
    assert set(dups[0]["duplicate_entry_ids"]) == {"e-6748", "e-6751"}
    buf = io.StringIO()
    with redirect_stdout(buf):
        cmd_pr._print_pr_sync_plan(actions)
    assert "同じ PR を指す記録が複数" in buf.getvalue(), buf.getvalue()


# --- 6. 突合は人が下した判断を上書きしない (2026-10-07 実データで発見) -------

def test_sync_does_not_resurrect_a_duplicate_someone_cancelled():
    """重複として捨てられた記録を「GitHub が MERGED だから」で done に戻さない。

    突合の規則は「GitHub が MERGED なら done」だが、この規則は重複を知らない。
    同じ PR を指す記録が 2 件あり片方を人が意図的に cancelled にしていた場合
    (= 重複として捨てた)、素直に規則を当てると 1 つの PR に done が 2 件並ぶ。

    実測 (2026-10-07): 状態を変える対象 69 件のうち 18 件が重複の組に入っており、
    うち 2 件 (e-884 PR#16 / e-6228 PR#732) は cancelled → done だった。
    e-884 は e-805 (done) の重複として捨てられたもの。

    「GitHub に合わせる」は、人が下した判断を上書きする理由にならない。
    """
    data = _data(
        _entry("e-805", "https://github.com/o/r/pull/16", 16, status="done"),
        _entry("e-884", "https://github.com/o/r/pull/16", 16, status="cancelled"),
    )
    gh = [{"number": 16, "state": "MERGED", "url": "https://github.com/o/r/pull/16"}]
    actions = core.plan_pr_sync(data, gh, fetched_floor=1)
    assert not any(a["action"] in ("merge", "close") and a["entry_id"] == "e-884"
                   for a in actions), (
        "重複として捨てられた記録を自動で動かそうとしている: " + repr(actions))
    # 報告はやめない (= この課題が直した「無言で飛ばして整合と報告する」への退行を
    # 防ぐ)。ただし ms-166 e-6941 で、**人が既に判断済み** のものは
    # `duplicate_human_decided` として、**これから決める必要がある** ものと別種別にした。
    # 混ぜると「決める必要がある件数」が出せず、整理しても報告が減らないので
    # 何件残っているのかを数える母集団が永久に出せなかった。
    reported = [a for a in actions if a["entry_id"] == "e-884"
                and a["action"] in ("duplicate_human_decided",
                                    "blocked_by_duplicate")]
    assert reported, "止めたことを報告していない: " + repr(actions)
    assert reported[0]["action"] == "duplicate_human_decided", (
        "人が理由をつけて取り消した記録を『これから決める必要がある』側に数えている: "
        + repr(reported))
    assert reported[0].get("blocked_reason"), "なぜ動かさないかを言っていない"
    core.apply_pr_sync(data, actions)
    assert data["milestones"][0]["entries"][1]["status"] == "cancelled", (
        "人が cancelled にした記録が done に戻された")


def test_a_record_outside_the_fetched_window_is_not_called_missing():
    """取得範囲の外にある記録を「GitHub に無い」と警告しない。

    gh pr list は新しい順に N 件しか返さない。窓の外になった古い記録を
    「見つからない」と報告すると偽警報になる — 実測 2026-10-07 に limit=100 で
    562 件が誤って警告された (この課題で入れた報告自身が、読まれなくなるほど
    騒ぐ側に倒れていた = 直した欠陥の裏返し)。
    """
    data = _data(_entry("e-old", "https://github.com/o/r/pull/2", 2))
    gh = [{"number": 700, "state": "MERGED", "url": "https://github.com/o/r/pull/700"}]
    actions = core.plan_pr_sync(data, gh, fetched_floor=700)
    kinds = {a["action"] for a in actions}
    assert "out_of_window" in kinds, repr(actions)
    assert "unmatched" not in kinds, (
        "窓の外を「見つからない」と報告している: " + repr(actions))
    buf = io.StringIO()
    with redirect_stdout(buf):
        cmd_pr._print_pr_sync_plan(actions)
    out = buf.getvalue()
    assert "⚠" not in out, "事実の報告に警告記号を付けている:\n" + out
    assert "範囲の外" in out, out


def test_long_listings_are_capped_but_counts_stay_exact():
    """件数は正確に、列挙は読める量で打ち切る。

    歴史的な負債は 50 組・69 件という規模で出る。毎回全部並べると読み手は報告自体を
    読まなくなり、「緑を信じる」のと同じ害に戻る。
    """
    entries = [_entry(f"e-{i}", f"https://github.com/o/r/pull/{i}", i)
               for i in range(100, 130)]
    data = _data(*entries)
    gh = [{"number": i, "state": "MERGED",
           "url": f"https://github.com/o/r/pull/{i}"} for i in range(100, 130)]
    actions = core.plan_pr_sync(data, gh, fetched_floor=100)
    buf = io.StringIO()
    with redirect_stdout(buf):
        cmd_pr._print_pr_sync_plan(actions)
    out = buf.getvalue()
    assert "(30 件)" in out, "件数が正確でない:\n" + out
    assert "他 20 件" in out, "残りの件数を言っていない:\n" + out
    # **行数を数える。** 「他 20 件」の有無だけを見る検査は、打ち切りを外しても
    # その行は出るので素通りする (最初にそう書いて mutation テストで気づいた)。
    listed = [ln for ln in out.splitlines() if ln.startswith("  [e-")]
    assert len(listed) <= 10, (
        f"列挙を打ち切っていない ({len(listed)} 行出ている):\n" + out)


# --- 7. 種別のカタログが consumer に行き渡っているか (独立保守性レビュー M-3) ---
#
# producer (plan_pr_sync) が出しうる種別の一覧と、2 つの consumer
# (apply_pr_sync / cmd_pr._print_pr_sync_plan) が知っている種別が食い違うと、
# **この課題が直した「無言で飛ばして嘘の整合を出す」欠陥が、種別のカタログという
# 1 段上の階層で再発する**。9 番目の種別を足した人が consumer を更新し忘れたら
# 落ちること。

def test_every_action_kind_is_known_to_the_printer():
    """表示側が全種別を分類していること。

    知らない種別は by_kind のどのバケツにも入らず、既存バケツが全部空なら
    「整合しています」が出てしまう (= 嘘の異常なし)。
    """
    import inspect
    src = inspect.getsource(cmd_pr._print_pr_sync_plan)
    shown = {k for k in core.PR_SYNC_ACTION_KINDS if f'"{k}"' in src}
    silent = set(cmd_pr._SILENT_ACTION_KINDS)
    missing = sorted(core.PR_SYNC_ACTION_KINDS - shown - silent)
    assert not missing, (
        "表示側が知らない action 種別があります — 既存バケツが空なら「整合しています」"
        "が出て、その種別の記録が黙って落ちます。出すか、意図して出さないなら "
        "cmd_pr._SILENT_ACTION_KINDS に理由付きで足してください: " + repr(missing))
    # 意図して出さない種別が、実在する種別であること (改名で腐らせない)
    assert silent <= core.PR_SYNC_ACTION_KINDS, sorted(silent - core.PR_SYNC_ACTION_KINDS)


def test_an_unknown_action_kind_is_not_silently_absorbed_by_apply():
    """適用側が未知の種別を黙って "skipped" に数えないこと。"""
    import pytest as _pytest
    data = _data(_entry("e-1", "https://github.com/o/r/pull/9", 9))
    bogus = [{"entry_id": "e-1", "pr_number": 9, "action": "teleport",
              "from_status": "in_review", "to_status": "done", "reason": "x"}]
    summary = core.apply_pr_sync(data, bogus)
    assert summary["errors"], (
        "未知の種別が黙って飲まれています (summary=" + repr(summary) + ")")
    assert "teleport" in str(summary["errors"]), summary["errors"]
    assert summary["skipped"] == 0, (
        "未知の種別を skipped に数えています: " + repr(summary))


def test_the_builder_refuses_a_kind_outside_the_catalog():
    """builder が一覧外の種別を拒否すること (producer 側の入口)。"""
    import pytest as _pytest
    with _pytest.raises(ValueError) as ei:
        core._pr_sync_action("teleport", entry_id="e-1", pr_number=1,
                             from_status="a", to_status="b", reason="x")
    assert "PR_SYNC_ACTION_KINDS" in str(ei.value)


def test_actionable_tells_a_consumer_what_to_do_without_the_catalog():
    """`--json` を読む外部の消費者が、種別を知らずに「何かすべきか」を判別できること。

    旧呼び出し側は `action != "skip"` を「実行対象」と読んでいた。報告だけの種別が
    増えた今それを踏むと、unreadable / duplicate まで「やるべき遷移」として拾う。
    """
    data = _data(_entry("e-1", "772"), _entry("e-2", "not-a-pr"))
    gh = [{"number": 772, "state": "MERGED",
           "url": "https://github.com/o/r/pull/772"}]
    actions = core.plan_pr_sync(data, gh, fetched_floor=1)
    for a in actions:
        assert "actionable" in a, "actionable が無い種別があります: " + repr(a)
        assert a["actionable"] == (a["action"] in core.PR_SYNC_TRANSITION_KINDS), a
    # 報告だけの種別が「やるべき遷移」に数えられないこと
    assert [a["action"] for a in actions if a["actionable"]] == ["merge"], (
        [a["action"] for a in actions if a["actionable"]])


def test_a_pending_duplicate_is_still_reported_as_needing_a_decision():
    """まだ誰も判断していない二重登録は『決める必要がある』側に残ること (e-6941)。

    取り消し済みを別種別に分けた副作用で、**本当に判断が要るものまで静かになる**
    のが怖い形 (= 緩すぎる側への倒れ込み。今日この repo で 2 回踏んだ)。
    片方が cancelled でなければ従来どおり blocked_by_duplicate で報告される、
    という境界をここで固定する。
    """
    data = _data(
        _entry("e-900", "https://github.com/o/r/pull/77", 77, status="in_review"),
        _entry("e-901", "https://github.com/o/r/pull/77", 77, status="in_review"),
    )
    gh = [{"number": 77, "state": "MERGED", "url": "https://github.com/o/r/pull/77"}]
    actions = core.plan_pr_sync(data, gh, fetched_floor=1)
    kinds = {a["entry_id"]: a["action"] for a in actions
             if a["entry_id"] in ("e-900", "e-901")
             and a["action"] != "duplicate"}
    assert set(kinds.values()) == {"blocked_by_duplicate"}, (
        "判断待ちの二重登録が『判断済み』側に落ちた: " + repr(actions))
    assert not any(a["action"] == "duplicate_human_decided" for a in actions), (
        "誰も取り消していないのに解決済みと報告した: " + repr(actions))


def test_resolving_a_duplicate_actually_reduces_the_pending_count():
    """取り消すと『決める必要がある件数』が実際に減ること (e-6941 の受入条件)。

    整理しても報告が減らないと、何件残っているのかを数える母集団が出せない。
    **減ることを前後比較で測る** — これが無いと「分けた」だけで終わる。
    """
    def plan(second_status):
        data = _data(
            _entry("e-910", "https://github.com/o/r/pull/88", 88, status="done"),
            _entry("e-911", "https://github.com/o/r/pull/88", 88,
                   status=second_status),
        )
        gh = [{"number": 88, "state": "MERGED",
               "url": "https://github.com/o/r/pull/88"}]
        acts = core.plan_pr_sync(data, gh, fetched_floor=1)
        return sum(1 for a in acts if a["action"] == "blocked_by_duplicate")

    before = plan("in_review")
    after = plan("cancelled")
    assert before == 1, f"判断待ちが 1 件であるべき: {before}"
    assert after == 0, (
        f"取り消しても判断待ちが減っていない ({before} → {after})")


# --- 二重登録を保存前に止める (ms-166 e-6903 の受入条件 3) --------------------
#
# 50 組 / 状態不一致 18 件は「作られてから気づく」形で積み上がった。既存の claim 競合
# 警告 (ms-80 e-1821) は「同じ MS に並列 open PR がある」だけを見て、**同じ PR 番号か
# は見ていない**。それがこの経路。気づいた時点で既に人の判断が必要な負債になっている
# ので、保存前に止める側へ倒した。

def _pr_entries_for(entries, number):
    from cmd_pr import _entries_for_pr_number
    return _entries_for_pr_number(_data(*entries), number)


def test_an_existing_record_for_the_same_pr_is_found():
    """同じ PR 番号の既存記録を見つけること。"""
    found = _pr_entries_for(
        [_entry("e-700", "https://github.com/o/r/pull/55", 55, status="done")],
        "55")
    assert [f["eid"] for f in found] == ["e-700"], found


def test_a_cancelled_record_does_not_block_recording_the_survivor():
    """取り消し済みの記録は邪魔をしないこと (e-6941 と整合)。

    人が理由をつけて捨てた記録は決定済みなので、これが残っていると `pr add` が
    永久に拒否する形になってはいけない。突合側が cancelled を終端として扱う
    (duplicate_human_decided) のと、ここで同じ扱いにしておかないと、**取り消した途端に
    突合は「解決済み」と言い、pr add は「重複だ」と言う**という食い違いが起きる。
    """
    found = _pr_entries_for(
        [_entry("e-701", "https://github.com/o/r/pull/56", 56,
                status="cancelled")],
        "56")
    assert found == [], (
        "取り消し済みの記録が新規登録を塞いでいる: " + repr(found))


def test_a_different_pr_number_is_not_treated_as_a_duplicate():
    """別の PR 番号を重複と見なさないこと (緩すぎる側の歯止め)。"""
    found = _pr_entries_for(
        [_entry("e-702", "https://github.com/o/r/pull/57", 57, status="done")],
        "58")
    assert found == [], found


def test_the_number_is_parsed_not_string_matched():
    """url 文字列の一致ではなく番号で突き合わせること。

    この repo には url 欄にフラグ文字列や説明文が入った記録が 17 件ある
    (ms-166 e-6902)。文字列比較だと本物の重複を取り逃すうえ、突合が鍵にしている
    のは番号の方なので、2 つの機構が別の鍵で動くことになる。
    """
    found = _pr_entries_for(
        [_entry("e-703", "https://github.com/o/r/pull/59", 59, status="done")],
        "59")
    assert [f["eid"] for f in found] == ["e-703"], found
    # 番号が読めない記録 (url にフラグ文字列) は、どの番号の重複でもない
    broken = _pr_entries_for(
        [_entry("e-704", "--intent-stdin", None, status="in_review")], "59")
    assert broken == [], broken


def test_pr_add_actually_routes_through_the_duplicate_check():
    """述語が**在る**だけでなく、`pr add` がそれを**通っている**ことを留める。

    上の 4 件は `_entries_for_pr_number` の論理を測るが、`cmd_pr_add` が呼ぶのを
    やめても緑のままになる (関数が定義されたまま未使用になるだけ)。反転で実測した:
    呼び出しを `existing = []` に差し替えても 24 件すべて緑だった。
    **「足した」と「全域に効く」は別**で、今日この repo で同型を 2 度踏んでいる。

    文字列の部分一致ではなく構文木から呼び出し名を抽出する (共有プリミティブ
    tests/_ast_structural.py)。docstring やコメントに名前が出ているだけで素通り
    するのが緩い一致の失敗形。
    """
    import ast

    from _ast_structural import called_names_in_function

    # パス定数を新設せず、import 済みの module から実ソースを取る (このファイルは
    # lib/ を sys.path に入れて cmd_pr を直接 import する作法)。
    import inspect
    src = inspect.getsource(cmd_pr)
    called = called_names_in_function(ast.parse(src), "cmd_pr_add")
    assert "_entries_for_pr_number" in called, (
        "pr add が二重登録の検知を通っていない — 50 組が作られた経路がそのまま "
        "開いている。実際に呼ばれている名前: " + repr(sorted(called)))


def test_the_duplicate_check_runs_before_the_write():
    """検知が **保存より前** に走ること。

    警告してから保存する形では手遅れ (= 作られてから気づく)。`cmd_pr_add` の中で
    `_entries_for_pr_number` の呼び出しが `save_project` より先に現れることを
    行番号で固定する。
    """
    import ast

    import inspect
    tree = ast.parse(inspect.getsource(cmd_pr))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "cmd_pr_add"),
              None)
    assert fn is not None, "cmd_pr_add が見つからない"
    check_at = save_at = None
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", "")
        if name == "_entries_for_pr_number" and check_at is None:
            check_at = node.lineno
        if name == "save_project" and save_at is None:
            save_at = node.lineno
    assert check_at is not None, "検知の呼び出しが無い"
    assert save_at is not None, "save_project の呼び出しが無い"
    assert check_at < save_at, (
        f"検知 (行 {check_at}) が保存 (行 {save_at}) より後に走っている — "
        "作られてから気づく形に戻っている")


# --- PR #793 の独立レビュー (AX high 2 件 + medium 1 件) ---------------------

def test_the_duplicate_check_reads_the_structured_number_first():
    """権威的な `meta.pr_number` を先に読むこと (AX high)。

    最初の版は url を再解析しており、**独立レビューが実測で見逃しを示した**:
    url が `.../pull/60.diff` (GitHub 自身が出す形。`.patch` や REST の
    `pulls/60` も同様) で `meta.pr_number == 60` が正しく入っている記録を
    見つけられず、新規登録がそのまま通った。url 欄に自由文が入った 17 件
    (ms-166 e-6902) も同じ経路で素通りする。

    **沈黙して発火しない拒否は、拒否が無いより悪い** — 呼び出し側は「この番号は
    守られている」と信じる。commit message に「番号で突き合わせる」と書きながら
    url を再解析していたのが実態だった。
    """
    for url in ("https://github.com/o/r/pull/60.diff",
                "https://github.com/o/r/pull/60.patch",
                "https://api.github.com/repos/o/r/pulls/60",
                "--intent-stdin"):
        data = _data(_entry("e-900", url, None, status="done"))
        # meta.pr_number を権威として入れる (core.pr_add が書く形)
        data["milestones"][0]["entries"][0].setdefault(
            "meta", {})["pr_number"] = 60
        found = cmd_pr._entries_for_pr_number(data, "60")
        assert [f["eid"] for f in found] == ["e-900"], (
            f"url={url!r} で見逃した (meta.pr_number=60 は入っている)")


def test_the_url_parse_is_still_a_fallback():
    """構造化フィールドが無い古い記録は url から読むこと (後方互換)。"""
    # `_entry` は url を meta.url に置く。pr_number だけ落として「構造化フィールドが
    # 無い古い記録」を作る (最初に書いた版は meta を丸ごと消して url ごと失い、
    # 実装ではなく試験の作りが誤っていた)。
    data = _data(_entry("e-901", "https://github.com/o/r/pull/61", None,
                        status="done"))
    data["milestones"][0]["entries"][0]["meta"].pop("pr_number", None)
    assert [f["eid"] for f in cmd_pr._entries_for_pr_number(data, "61")] == [
        "e-901"], "構造化フィールドが無い古い記録を url から読めていない"


def test_the_live_path_does_not_claim_alignment_while_issues_remain(capsys):
    """書き込む経路が、論点を残したまま「整合しています」と言わないこと (AX high)。

    以前は `merged == 0 and closed == 0` だけを見て
    「All beacon PR entries are already aligned with GitHub.」を出していた。
    人の判断待ちの二重登録 8 件・判別できない記録 17 件が残っていても嘘の整合を
    宣言する形で、**dry-run 側だけ直して書き込む側が直っていなかった** —
    e-6871 で直した「無言で飛ばして整合と報告する」が、もう一方の消費者で
    生き残っていた。

    **実挙動で測る。** 最初に書いた版は「種別名の文字列が関数内に在るか」を構文木で
    見ており、判定条件を元に戻しても文字列は残るので緑のままだった (反転 U2 で
    判明)。今日 2 度言われた緩い一致をまた書いていた。実際に関数を走らせて、
    出力に「整合しています」が出ないことを測る。
    """
    import io
    from contextlib import redirect_stdout

    # 判断待ちの二重登録が 1 組ある状態 (= 状態は動かないが論点は残る)
    data = _data(
        _entry("e-920", "https://github.com/o/r/pull/99", 99,
               status="in_review"),
        _entry("e-921", "https://github.com/o/r/pull/99", 99,
               status="in_review"),
    )
    gh = [{"number": 99, "state": "MERGED",
           "url": "https://github.com/o/r/pull/99"}]
    actions = core.plan_pr_sync(data, gh, fetched_floor=1)
    summary = core.apply_pr_sync(data, actions)
    assert summary["merged"] == 0 and summary["closed"] == 0, (
        "この fixture は状態が動かない前提: " + repr(summary))
    assert any(a["action"] == "blocked_by_duplicate" for a in actions), (
        "判断待ちが報告されていない前提が崩れている")

    # 書き込む経路と同じ判定を通す (cmd_pr_sync の非 json 分岐が使う条件)
    buf = io.StringIO()
    with redirect_stdout(buf):
        cmd_pr._print_pr_sync_outcome(actions, summary)
    out = buf.getvalue()
    assert "already aligned with GitHub" not in out, (
        "論点が残っているのに整合を宣言した:\n" + out)
    assert "blocked_by_duplicate" in out or "判断" in out, (
        "何が残っているかを言っていない:\n" + out)


def test_the_live_path_does_claim_alignment_when_nothing_remains(capsys):
    """論点が無いときは素直に整合を名乗ること (厳しすぎる側の歯止め)。

    全部に ⚠ を付けると読み手が報告を読まなくなり、「緑を信じる」のと同じ害に
    戻る。何も残っていなければ従来どおり整合と言う。
    """
    import io
    from contextlib import redirect_stdout

    data = _data(_entry("e-930", "https://github.com/o/r/pull/100", 100,
                        status="done"))
    gh = [{"number": 100, "state": "MERGED",
           "url": "https://github.com/o/r/pull/100"}]
    actions = core.plan_pr_sync(data, gh, fetched_floor=1)
    summary = core.apply_pr_sync(data, actions)
    buf = io.StringIO()
    with redirect_stdout(buf):
        cmd_pr._print_pr_sync_outcome(actions, summary)
    assert "already aligned with GitHub" in buf.getvalue(), buf.getvalue()


def test_the_human_decided_kind_is_not_named_resolved():
    """「解消された」と読める名前にしないこと (AX medium)。

    重複の構造は消えない — 両方の記録は恒久的に残り、解消されたのは「どちらを
    残すか」の人の判断だけ。`..._resolved` だと外部 consumer が「クリーンアップ
    済みだから数えなくてよい」と早合点しうる。
    """
    kinds = {k for k in core.PR_SYNC_ACTION_KINDS}
    assert "duplicate_human_decided" in kinds, kinds
    assert not any(k.endswith("_resolved") for k in kinds), (
        "『解消された』と読める種別名が在る: " + repr(sorted(kinds)))


def test_every_report_kind_blocks_the_alignment_claim():
    """**種別ごとに** 整合の宣言を止めること (網羅の実測)。

    1 種別だけで駆動すると、網羅リストから他の種別を落としても緑のままになる
    (反転 U2b で実測: `duplicate` だけ残して他を落としても 31 件すべて緑だった)。
    「足した」と「全種別に効く」は別で、今日この repo で同型を何度も踏んでいる。

    報告だけの種別それぞれについて、それ 1 つが在るだけで整合を名乗らないことを
    1 件ずつ測る。
    """
    import io
    from contextlib import redirect_stdout

    kinds = ["unreadable", "unmatched", "duplicate", "blocked_by_duplicate",
             "duplicate_human_decided"]
    zero = {"merged": 0, "closed": 0, "skipped": 0}
    for kind in kinds:
        actions = [{"entry_id": "e-x", "action": kind, "pr_number": 1,
                    "from_status": "in_review", "to_status": "in_review",
                    "reason": "probe", "duplicate_entry_ids": ["e-x", "e-y"]}]
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_pr._print_pr_sync_outcome(actions, dict(zero))
        assert "already aligned with GitHub" not in buf.getvalue(), (
            f"種別 {kind!r} が残っているのに整合を宣言した:\n" + buf.getvalue())

    # 境界: 報告だけの種別が 1 つも無ければ整合を名乗る
    buf = io.StringIO()
    with redirect_stdout(buf):
        cmd_pr._print_pr_sync_outcome(
            [{"entry_id": "e-z", "action": "skip", "pr_number": 2,
              "from_status": "done", "to_status": "done",
              "reason": "already merged in beacon"}], dict(zero))
    assert "already aligned with GitHub" in buf.getvalue(), buf.getvalue()


def test_the_catalog_and_the_alignment_check_cannot_drift():
    """網羅リストが種別カタログから外れていないことを機械で測る。

    上の試験は「いま在る種別それぞれが効く」ことを測るが、**新しい種別が
    カタログに足されたとき**に網羅リストへ入れ忘れる経路は塞げない。
    カタログ側を真値として、報告だけの種別が全部入っているかを突き合わせる。
    """
    import ast
    import inspect

    fn = next((n for n in ast.walk(ast.parse(inspect.getsource(cmd_pr)))
               if isinstance(n, ast.FunctionDef)
               and n.name == "_print_pr_sync_outcome"), None)
    assert fn is not None
    listed = {n.value for n in ast.walk(fn)
              if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    # 状態を動かす種別 (merge / close) と skip 系はこの判定の対象外
    report_only = set(core.PR_SYNC_ACTION_KINDS) - {
        "merge", "close", "skip", "out_of_window"}
    missing = report_only - listed
    assert not missing, (
        "カタログに在るが整合判定が見ていない種別: " + repr(sorted(missing)))
