"""宛先の候補一覧が「詰まっている相手」を健全の顔で出さないことの試験 (ms-166 e-6778)。

何が起きていたか
----------------
directory の row は ``reachable`` (= live かつ確定的に詰まっていない) を計算して JSON にも
載せているのに、**人間と AI が宛先を選ぶ行には出ていなかった**。``--healthy`` の filter も
これを見ない。つまり受信が進んでいない相手が「healthy」の顔で候補に並ぶ。

2026-10-01 に実測され、報告者は「生の JSON を目視したから気づいた」と書いている。
``/beacon-dm-send`` の手順に従う限り気づけないのが問題の本質 — 配線はあるのに、
選ぶ画面に届いていない。

倒し方
------
**見せて選ばせる。** ``--healthy`` の filter に織り込んで候補から落とす案は採らない:
候補が黙って消えると「なぜ出てこないのか」が分からず、無言で落とすという同じ型の害に
なる (ms-178 が fork 一覧で採った方針と同じ)。

印を付けるのは ``reachable`` が **false のときだけ**。契約 (bus_liveness.is_reachable)
では ``None`` は「詰まっているか不明」で live 扱いなので、そこに印を付けると待ち行列が
空なだけの idle な相手まで警告だらけになる。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import bus_liveness  # noqa: E402

_SRC = os.path.join(os.path.dirname(__file__), "..", "lib", "cmd_bus.py")


def _renderer_source() -> str:
    """候補一覧を描く部分のソース。

    実際に CLI を起動すると cloud / gh / directory endpoint が要るので、ここは
    「描画に reachable が配線されているか」を構造で見る。振る舞いの側は
    tests/test_bus_cli.py の既存ケースが担う。
    """
    return open(_SRC, encoding="utf-8").read()


def test_the_picker_row_carries_the_wedged_marker():
    src = _renderer_source()
    assert "reach_tag" in src, (
        "候補一覧の行に reachable の印が配線されていません — 詰まっている相手が "
        "healthy の顔で並びます (ms-166 e-6778)")
    # 印が **行の出力に実際に入っている** こと (変数を作っただけで使っていない形を弾く)
    printed = [ln for ln in src.splitlines()
               if "print(" in ln or "f\"" in ln]
    assert any("{reach_tag}" in ln for ln in printed), (
        "reach_tag を組み立てているが行の出力に入れていません")


def test_the_marker_fires_only_on_a_definitive_wedge():
    """``reachable is False`` のときだけ印が出ること。

    契約上 ``None`` は「不明」で live 扱い。そこに印を付けると、待ち行列が空なだけの
    idle な相手まで警告だらけになり、印の意味が薄まる。
    """
    src = _renderer_source()
    assert 's.get("reachable") is False' in src, (
        "reachable の真偽を `is False` で判定していません — truthy 判定だと None "
        "(= 不明) まで詰まり扱いになり、idle な相手が警告だらけになります:\n"
        "契約は bus_liveness.is_reachable を参照")
    # 契約そのものを留める (ここが変わったら印の意味も変わる)
    assert bus_liveness.is_reachable(True, None) is True, "None は live 扱いのはず"
    assert bus_liveness.is_reachable(True, False) is False, "確定的な詰まりは false"
    assert bus_liveness.is_reachable(False, None) is False, "live でなければ false"


def test_the_filter_still_does_not_silently_drop_wedged_candidates():
    """``--healthy`` が詰まっている相手を候補から落とさないこと。

    落とす形を選ばなかったのは、候補が黙って消えると「なぜ出てこないのか」が
    分からず、この課題が直している「無言で落とす」と同じ型になるから。
    この試験は **その設計判断を固定する** (将来 filter に織り込みたくなった人に、
    なぜそうしなかったかを突き当たらせる)。
    """
    src = _renderer_source()
    # healthy の filter は poll_health だけを見る (reachable を混ぜない)
    i = src.index("healthy_only = os.environ.get")
    window = src[i:i + 1200]
    assert "reachable" not in window, (
        "--healthy の filter に reachable を織り込んでいます。候補から黙って落とすと "
        "「なぜ出てこないのか」が分からず、無言で落とす害に戻ります。"
        "見せて選ばせる形を維持してください (ms-166 e-6778)")


def test_the_skill_tells_the_operator_about_the_marker():
    """手順書にも届いていること (3 コピーすべて)。

    CLI が印を出しても、Skill の手順が「healthy なら送ってよい」と読める書き方のまま
    だと、手順どおり進めた人は印の意味を知らない。報告者が「手順に従う限り気づけない
    のが本質」と書いたのはそこ。
    """
    root = os.path.join(os.path.dirname(__file__), "..")
    copies = ["skills/beacon-dm-send.md",
              "shared/skills/beacon-dm-send/SKILL.md",
              "plugins/beacon/skills/beacon-dm-send/SKILL.md"]
    for rel in copies:
        path = os.path.join(root, rel)
        assert os.path.exists(path), "Skill のコピーが見つかりません: " + rel
        body = open(path, encoding="utf-8").read()
        assert "詰まり" in body, (
            rel + " に詰まりの印の説明がありません — 3 コピーは同期が要ります "
            "(正本は skills/<name>.md、配布は shared/ と plugins/)")
        assert "reachable" in body, rel + " に reachable の語がありません"
