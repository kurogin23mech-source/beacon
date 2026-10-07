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
    assert any(a["action"] == "blocked-by-duplicate" and a["entry_id"] == "e-884"
               for a in actions), "止めたことを報告していない: " + repr(actions)
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
    assert "out-of-window" in kinds, repr(actions)
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
