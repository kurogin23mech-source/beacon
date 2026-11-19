"""記録のみの気づきが「直す候補」として数えられないことを固定する (ms-119 e-7554).

背景
====
4 つのレビュー (目的達成 / 思想 / AX / 保守性) は、judge の出力を 2 本の配列で返す:

* ``findings``     — 直さなければ困るもの (直す候補)
* ``record_only``  — 直さなくてもシステムの動きが変わらないもの (記録して終わり)

配列を分けたのは、同じ配列に印を付けて並べる形では、受け取る側が全部を修正依頼として
読んでしまったから (2026-11-19: 型注釈の引用符が 1 箇所だけ違うという指摘が、黙って
失敗する欠陥と同じ列に並んでいた)。

**この試験が守る性質**: 畳み込み (``aggregate_review_reports``) は ``findings`` だけを
読み、``record_only`` を畳まれた一覧にも種別ごとの件数にも入れない。

なぜ試験が要るのか
==================
いまこの性質が成り立っているのは、畳み込みが ``rep.get("findings")`` しか読まず、
件数も ``len(findings)`` で数えているから **だけ** である。契約として書かれた場所は
どこにも無い。畳み込みを「返ってきた配列を全部拾う」形に変えると、記録のみが直す
候補として数え直され、**散文の規則は何も変わっていないのに過剰検出が戻る**。

だからここで挙動として固定する。分類規則そのもの (「この指摘は軽いか」) は散文で
測らない —— 緩い一致で軽重を判定する検査は、このリポジトリが繰り返し作ってきた
偽の赤と偽の緑を生む型なので、規則の妥当性はレビューに委ねる。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

import review_spine  # noqa: E402


#: 直す候補 (動作が変わる = 黙って失敗する欠陥)
_FINDING = {
    "title": "結合が散在していて局所だけ読んでは安全に変えられない",
    "file": "lib/a.py",
    "line": 10,
    "severity": "high",
}

#: 記録のみ (動作は変わらない = 表記の揺れ)
_RECORD_ONLY = {
    "title": "型注釈の引用符が 1 箇所だけ揃っていない",
    "file": "lib/a.py",
    "line": 120,
}


def _titles(folded: dict) -> list:
    return [f.get("title") for f in folded.get("findings") or []]


def test_record_only_is_not_folded_into_findings():
    """記録のみは畳まれた一覧に現れない。"""
    folded = review_spine.aggregate_review_reports([
        {"review_type": "maintainability",
         "findings": [_FINDING],
         "record_only": [_RECORD_ONLY]},
        {"review_type": "ax",
         "findings": [],
         "record_only": [_RECORD_ONLY]},
    ])
    titles = _titles(folded)
    assert _FINDING["title"] in titles, (
        "直す候補が畳み込みから落ちている (これが落ちると本物の欠陥が消える)")
    assert _RECORD_ONLY["title"] not in titles, (
        "記録のみが直す候補の一覧に混ざった。配列を分けた意味が無くなり、"
        "受け取る側は全部を修正依頼として読む (e-7554 の起点そのもの)")


def test_record_only_is_not_counted_per_type():
    """種別ごとの件数も記録のみを数えない。

    件数は「この種別は何件直せと言ってきたか」を表す。記録のみを数えると、
    直す候補がゼロの種別が「1 件指摘あり」に見える。
    """
    folded = review_spine.aggregate_review_reports([
        {"review_type": "ax", "findings": [], "record_only": [_RECORD_ONLY]},
    ])
    assert folded["by_type"]["ax"] == 0, (
        f"直す候補ゼロの種別が {folded['by_type']['ax']} 件と数えられた "
        "(記録のみが件数に混ざっている)")


def test_the_assertions_go_red_when_the_property_breaks():
    """**この試験自身を検証する。**

    上の 2 本が「測れている」ことを、性質を壊した入力で確かめる。記録のみを直す
    候補の列に移した形 (= 畳み込みが全配列を拾うように変わったときに起きること) を
    与えて、同じ判定が赤くなることを見る。

    反転を入れておく理由: 不在を測る検査は、測り方が壊れても緑のままになる。
    「混ざっていない」は、そもそも何も見ていなくても成り立ってしまう。
    """
    broken = review_spine.aggregate_review_reports([
        {"review_type": "maintainability",
         "findings": [_FINDING, _RECORD_ONLY],
         "record_only": []},
        {"review_type": "ax",
         "findings": [_RECORD_ONLY],
         "record_only": []},
    ])
    assert _RECORD_ONLY["title"] in _titles(broken), (
        "反転が効いていない。記録のみを findings に入れたのに畳み込みに現れない "
        "= 上の 2 本は性質を見ていない可能性がある")
    assert broken["by_type"]["ax"] == 1, (
        "反転が効いていない。件数が記録のみを数えていないなら、"
        "test_record_only_is_not_counted_per_type の緑は無意味")
