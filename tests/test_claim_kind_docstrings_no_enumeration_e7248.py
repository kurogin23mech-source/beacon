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
    """説明文の中で「種別名が区切りだけで 3 種類以上つながっている」箇所を返す。

    測るのは **列挙の形そのもの**: 種別名と種別名の間にあるのが区切り (``/`` ``,``
    ``|`` 空白 バッククォート ``and`` ``or``) **だけ** のとき、それは語彙を手書きで
    並べた形である。意味のある語が挟まっていれば散文であって列挙ではない。

    「近さ」で測る形は捨てた。独立レビュー 3 周目が、距離 + 相異なる 3 種では
    ``differ by exactly the retired ``trek`` kind; a free kind is not a task`` のような
    **散文が赤くなる**ことを実測した。赤くなる側だけを合わせると、記述を書かせない圧力に
    なって「何が在ったか」の記録を失わせる —— この試験が守りたいものを壊す。

    ``ms-185`` のようなマイルストーン ID の ``ms`` も数えない (``-数字`` が続く形を除外)。
    このリポの説明文は ms-ID の引用が慣例で、測定対象の 3 関数自身がそう書いている。
    """
    pat = r"\b(" + "|".join(re.escape(k) for k in ALL_KINDS) + r")\b(?!-\d)"
    hits = [(m.start(), m.end(), m.group(1)) for m in re.finditer(pat, doc)]
    #: 種別名の間に在ってよい「区切りだけ」の形。これ以外の語が挟まれば散文。
    sep = re.compile(r"^[\s/,|`'\"()\[\]・、]*(?:and|or|/)?[\s/,|`'\"()\[\]・、]*$")
    runs, cur = [], []

    def _flush(group):
        if len({k for _, _, k in group}) >= 3:
            runs.append([k for _, _, k in group])

    for h in hits:
        if cur and sep.match(doc[cur[-1][1]:h[0]]):
            cur.append(h)
        else:
            _flush(cur)
            cur = [h]
    _flush(cur)
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


@pytest.mark.parametrize("doc", [
    "Public accessor for the vocabulary (``ms`` / ``task`` / ``operation`` / ``trek`` / ``free``) — see above.",
    "kinds: ms, task, operation, free",
    "one of ms|task|operation|free",
    "ms / task / operation and free",
    "種別は ms・task・operation・free",
])
def test_the_check_itself_catches_a_real_enumeration(doc):
    """test-the-test: 実際に並べた文面なら検知できることを確かめる。

    検査が「何も見つけない」だけなら、壊れた検査と区別がつかない。区切りは `/` `,`
    `|` `・` と `and` を網羅する (= 1 形だけ測ると別の区切りで素通りする)。
    """
    assert _enumeration_runs(doc), f"列挙を検知できていない: {doc!r}"


def test_a_single_historical_mention_is_allowed():
    """撤去を説明する歴史の記述 1 件は通る (= 記録を失わせない)。"""
    ok = "ms-185 removed ``trek`` from the set, so this accessor no longer returns it."
    assert not _enumeration_runs(ok), "歴史の記述で誤検知している"


@pytest.mark.parametrize("doc", [
    # 独立レビュー 3 周目が実測した偽陽性。どれも文面に列挙は無い。
    "ms-185 removed ``trek`` from the set; ms-109 e-5692 introduced the accessor.",
    "See ms-109, ms-112 and ms-185 for the history",
    "ms-185 で trek を外した (ms-109 e-5692 の accessor)",
    "differ by exactly the retired ``trek`` kind; a free kind is not a task",
])
def test_history_and_prose_do_not_false_positive(doc):
    """列挙でない散文で赤くならないこと (= 偽陽性側の固定)。

    赤くなる側だけを測ると、記述を書かせない圧力になって「何が在ったか」の記録を
    失わせる。この試験の初版は実際にこの 4 例すべてで赤くなった。
    """
    assert not _enumeration_runs(doc), f"列挙でない散文で誤検知: {doc!r}"
