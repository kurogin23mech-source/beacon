"""ms-185: 受信フックから「産出者ゼロの判定」を外したことを固定する。

独立した思想レビューが、`bin/beacon-bus-inbox-hook.py` が archived-trek フィルタを
main loop に配線したまま残していると指摘した。このフィルタは trek-* channel の event
ごとに `GET /api/treks/{id}` を引き、archived な trek の event を AI に見せず落とす層
だった。それを書いていた trek scheduler は e-7070 で撤去済みなので、**判定する相手が
居ない** —— SPEC 方針 4 が名指しで避けた「動かない配線 / 誰もセットしない判定」そのもの。

退役 channel が万一届いた場合の扱いは、落とすのではなく **開示する** (e-7079 が決めた
受信側の振る舞い)。したがってこの試験が固定するのは 2 つで、**両方そろって初めて意味が
ある**:

  - フィルタ層が消えていること (= 無駄な API 往復と、効いているつもりの層が無い)
  - 開示の分岐が残っていること (= 届いた退役 channel が黙って消えない)

片方だけを測ると、「フィルタを消したが開示も一緒に落とした」= 受信が黙って消える退行を
緑のまま通してしまう。
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(ROOT, "bin", "beacon-bus-inbox-hook.py")


@pytest.fixture(scope="module")
def hook_src():
    with open(HOOK, encoding="utf-8") as f:
        return f.read()


@pytest.mark.parametrize("name", [
    "_is_trek_scheduler_event",
    "_lookup_trek_status",
    "_filter_archived_trek_events",
    "_TREK_SCHEDULER_CHANNELS",
])
def test_filter_layer_is_gone(hook_src, name):
    """フィルタ層の 3 helper と、産出者を指す名前の集合が残っていない。"""
    assert name not in hook_src, (
        f"{name} が残っています。産出者 (trek scheduler) は e-7070 で撤去済みなので、"
        f"この判定は相手が居ません"
    )


def test_no_per_event_trek_lookup(hook_src):
    """event ごとに trek を API で引く **コード** が無い。

    helper 名の不在だけでは足りない: 同じ往復を別の名前で書き直しても『消えた』と
    名乗れてしまうので、URL の形そのものを見る。

    ただし **散文は除く** (コメントも docstring も)。この検査の初版は素朴に全文から
    `/api/treks/` を探し、撤去を説明する自分の退役注記に反応して赤くなった。注記で
    赤くなる検査は、注記を書かせない圧力になって「何が在ったか」の記録を失わせる。逆に
    「注記なら許す」と緩めると、言い換えた説明が素通りする (この MS が 5 周戦った形)。
    そこで主張を **コードに限る** —— 散文が正しいかは機械では閉じられないので、ここでは
    約束しない。

    除外は `tokenize` で COMMENT と STRING を落とす形にしている。2 版目は `#` で始まる
    行だけを落としており、**約束 (「コメントは除く」) より実装が狭かった** —— docstring
    形式の退役注記では赤くなるので、約束を読んで注記を書いた次の AI が理由の分からない
    赤を踏む (独立レビュー 3 周目の指摘)。
    """
    # 散文を **全部** 落とす: `#` のコメントだけでなく docstring も。
    # 初版は `#` 行だけを落としており、約束 (「コメントは除く」) より実装が狭かった。
    # このフックの退役注記は現に docstring 形式で書かれている (TREK_RETIRED_CHANNELS
    # の説明が関数の docstring に入る流儀) ので、次の注記もその形になる可能性が高く、
    # そこで赤くなると「注記を消す」が最も安い出口になる — この検査が避けたかった圧力
    # そのもの (独立レビュー 3 周目の指摘)。tokenize なら両方を機械的に落とせる。
    import io
    import tokenize
    pieces = []
    for tok in tokenize.generate_tokens(io.StringIO(hook_src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        pieces.append(tok.string)
    code = "\n".join(pieces)
    assert "/api/treks/" not in code, (
        "受信フックのコードが trek を API で引いています。読み出し面はサーバと CLI に"
        "在り、受信フックが event ごとに引く理由は無くなりました"
    )


def test_retired_channel_is_still_disclosed_via_the_downgrade_path():
    """届いた退役 channel が黙って消えないこと —— **実際に通る経路で**測る。

    この試験の初版はソースに ``elif channel in TREK_RETIRED_CHANNELS:`` の文字列が
    在ることで「開示は生きている」と主張していた。独立 AX レビューがその分岐が
    **到達不能**であることを実測した: ``classify_auto_execute`` は退役 channel を
    ``auto-execute`` 判定の前に ``propose-to-ai`` へ降格するので、``elif delivery ==
    "auto-execute":`` の内側に在るあの分岐には一度も入らない。つまり**私が書いた
    その試験は偽の緑**で、消えた開示を「生きている」と報告しうる形だった。

    なので測るのは文字列ではなく挙動にする: 退役 channel の event が降格され、
    降格理由が付くこと。これが成り立つ限り event は generic な propose-to-ai の
    一覧に乗って AI に見える。
    """
    import bus_delivery

    ev = {
        "channel": "trek-trigger",
        "payload": {"trek_id": "tk-1"},
        "delivery": "auto-execute",
        "envelope": {"tier": "T2", "scope": "trek:tk-1", "signature": "x"},
    }
    # allowlist に入れても (= 人間が opt-in しても) 降格されるのが退役の意味。
    delivery, downgraded_from, reason = bus_delivery.classify_auto_execute(
        ev, allowlist=frozenset({"trek-trigger"}))
    assert delivery == "propose-to-ai", delivery
    assert downgraded_from == "auto-execute", downgraded_from
    assert reason == "retired-provenance-channel", reason
    # downgraded_from が立つことが「generic 経路で開示される」条件そのもの
    # (フック側は `if downgraded_from:` で印を付けて一覧に乗せる)。
    assert downgraded_from, "降格印が付かないと開示されずに消える"


def test_the_unreachable_disclosure_branch_is_gone(hook_src):
    """到達不能だった専用分岐と、その写しの集合が残っていないこと。

    同じ 4 名の literal がフックと ``lib/bus_delivery`` の 2 箇所に在り、片方だけ
    変えても何も赤くならなかった。正本を lib 1 箇所に寄せたので、フック側に literal が
    戻っていないことを測る。
    """
    assert "TREK_RETIRED_CHANNELS = {" not in hook_src, (
        "退役 channel の literal がフックに戻っています。正本は "
        "lib/bus_delivery.RETIRED_PROVENANCE_CHANNELS の 1 箇所です"
    )
    assert "elif channel in TREK_RETIRED_CHANNELS:" not in hook_src, (
        "到達不能な分岐が戻っています (降格が先に起きるので入れません)"
    )
    assert "_format_trek_retired_block" not in hook_src, (
        "誰にも見えない開示ブロックが戻っています"
    )


def test_hook_still_parses():
    """構文が壊れていない (= 155 行を落としたので形を実際に確かめる)。"""
    import ast
    with open(HOOK, encoding="utf-8") as f:
        ast.parse(f.read())
