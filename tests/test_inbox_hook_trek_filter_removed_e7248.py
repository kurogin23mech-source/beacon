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

    ただし **コメントは除く**。この検査の初版は素朴に全文から `/api/treks/` を探し、
    撤去を説明する自分の退役注記に反応して赤くなった。注記で赤くなる検査は、注記を
    書かせない圧力になって「何が在ったか」の記録を失わせる。逆に「注記なら許す」と
    緩めると、言い換えた説明が素通りする (この MS が 5 周戦った形)。そこで主張を
    **コードに限る** —— 散文が正しいかは機械では閉じられないので、ここでは約束しない。
    """
    code = "\n".join(
        line for line in hook_src.splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "/api/treks/" not in code, (
        "受信フックのコードが trek を API で引いています。読み出し面はサーバと CLI に"
        "在り、受信フックが event ごとに引く理由は無くなりました"
    )


def test_retired_channel_disclosure_survives(hook_src):
    """開示の分岐は残っている (= 届いた退役 channel が黙って消えない)。"""
    assert "TREK_RETIRED_CHANNELS = {" in hook_src, "退役 channel の集合が無い"
    assert "elif channel in TREK_RETIRED_CHANNELS:" in hook_src, (
        "退役 channel の開示分岐が無い。フィルタと一緒に開示まで落とすと、"
        "届いた event が黙って消える退行になります"
    )


def test_retired_channel_set_is_the_four(hook_src):
    """集合の中身が 4 つのまま (= 畳む時に取りこぼしていない)。"""
    import re
    m = re.search(r"TREK_RETIRED_CHANNELS = \{(.*?)\}", hook_src, re.DOTALL)
    assert m, "集合の宣言が読めない"
    found = set(re.findall(r'"([a-z-]+)"', m.group(1)))
    assert found == {
        "trek-progress-check", "trek-task-review",
        "trek-leader-digest", "trek-trigger",
    }, found


def test_hook_still_parses():
    """構文が壊れていない (= 155 行を落としたので形を実際に確かめる)。"""
    import ast
    with open(HOOK, encoding="utf-8") as f:
        ast.parse(f.read())
