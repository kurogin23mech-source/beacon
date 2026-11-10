"""ms-185 / PR#840: claim の語彙を「説明文が手書きで並べる」形をやめたことを固定する。

PR#840 の独立レビュー 2 体が同じ欠陥を挙げた (consensus 2/2): `_VALID_TARGET_KINDS`
から `trek` を外したのに、その集合を返す公開アクセサ `claims.valid_target_kinds()` の
説明文が『ms / task / operation / trek / free』と並べ続けていた。同じ食い違いが
`occupation.non_claimable_protocol_kinds()` にもあり、そちらは除外される理由まで
誤って説明していた。

**その関数は「語彙を知る場所」であることが存在理由**なので、ここが嘘をつくと設計の
前提が崩れる。AI が説明文を正しい事前知識として読むと、`ValueError` を受け取ったときに
『claims.py 側のバグだ』と誤診して trek を valid に戻す修正を提案しうる。

## なぜ「trek という語の不在」を測らないのか

それは同じ型の罠を作る。撤去を説明する歴史の記述 (「ms-185 で trek を外した」) は
**残すべき情報**で、語の不在を測るとその記述で赤くなる。赤くなる検査は記述を書かせない
圧力になって「何が在ったか」の記録を失わせる。

そこで測るのは **列挙の形** にする: 既知の種別名が近接して 3 つ以上並んでいたら、それは
現在の語彙を手書きで並べた形であり、集合が変わったときに取り残される。1 つだけの言及
(= 歴史の記述) は通る。
"""
import inspect
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import claims  # noqa: E402
import occupation  # noqa: E402

#: 語彙の全候補 (書き込み集合 + 読み取り専用の退役分)。測る対象はこの語の並び。
ALL_KINDS = sorted(claims.readable_target_kinds())

ACCESSORS = [
    ("claims.valid_target_kinds", claims.valid_target_kinds),
    ("claims.readable_target_kinds", claims.readable_target_kinds),
    ("occupation.non_claimable_protocol_kinds", occupation.non_claimable_protocol_kinds),
]


def _enumeration_runs(doc: str) -> list:
    """説明文の中で「種別名が近接して 3 つ以上並ぶ」箇所を返す。

    近接の定義は『直前の種別名から 40 文字以内に次が現れる』。区切りが `/` でも `,` でも
    ``ms`` のような装飾付きでも拾えるようにしたいので、区切り文字ではなく距離で測る。
    """
    hits = [(m.start(), m.group(1)) for m in
            re.finditer(r"\b(" + "|".join(re.escape(k) for k in ALL_KINDS) + r")\b", doc)]
    runs, cur = [], []
    for pos, kind in hits:
        if cur and pos - cur[-1][0] <= 40:
            cur.append((pos, kind))
        else:
            if len(cur) >= 3:
                runs.append([k for _, k in cur])
            cur = [(pos, kind)]
    if len(cur) >= 3:
        runs.append([k for _, k in cur])
    return runs


@pytest.mark.parametrize("name,fn", ACCESSORS, ids=[n for n, _ in ACCESSORS])
def test_docstring_does_not_hand_enumerate_the_vocabulary(name, fn):
    doc = inspect.getdoc(fn) or ""
    runs = _enumeration_runs(doc)
    assert not runs, (
        f"{name} の説明文が語彙を手書きで並べています: {runs}。"
        f"集合が変わったときに取り残されるので、関係だけを述べて具体名は正本 "
        f"(_VALID_TARGET_KINDS / _READABLE_TARGET_KINDS) に持たせてください"
    )


def test_the_check_itself_catches_a_real_enumeration():
    """test-the-test: 実際に並べた文面なら検知できることを確かめる。

    検査が「何も見つけない」だけなら、壊れた検査と区別がつかない。
    """
    bad = "Public accessor for the vocabulary (ms / task / operation / trek / free) — see above."
    assert _enumeration_runs(bad), "列挙を検知できていない (= 検査が壊れている)"


def test_a_single_historical_mention_is_allowed():
    """撤去を説明する歴史の記述 1 件は通る (= 記録を失わせない)。"""
    ok = "ms-185 removed ``trek`` from the set, so this accessor no longer returns it."
    assert not _enumeration_runs(ok), "歴史の記述で誤検知している"
