"""ms-185: Trek を畳んだあと、claim の対象種別 `trek` が両方向で正しく扱われること。

独立した目的達成レビューが「消した面が help / README から参照されたまま残っている」
(受入条件 2) として、`beacon claim request trek:<id>` が**まだ通る**ことを指摘した。
Trek は作れなくなったので、新規 claim は拒否されなければならない。

ただし単に語彙から消すだけでは、`parse_claim_event` が書き込みと同じ集合を見ている
ため **ms-185 より前に記録された trek claim が黙って読めなくなる**。それは ms-185 が
「読み出し面は残す」と決めた判断 (dec-0a8b06a335785fde / dec-f9cec26cc2a4c8e4) に反する。
そこで語彙を 2 つに割った。この試験は **両方向** を固定する:

  - 新規作成は拒否される (= 作れない対象に鍵をかけられない)
  - 過去の記録は読める (= 黙って読めなくしない)

片方だけを測ると、どちらの穴も「直した」と名乗れてしまう。
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import claims  # noqa: E402


def test_new_trek_claim_is_refused():
    """作れない対象に新しく claim を立てられない。"""
    with pytest.raises(ValueError) as ei:
        claims.build_claim_payload(
            claim_kind="request", from_session_id="sv-1",
            target_kind="trek", target_id="tk-abc123",
        )
    # エラー文は受理する語彙を列挙する。trek がそこに出ないことまで見る
    # (列挙だけ直して集合を直さない / 集合だけ直して列挙が古い、の両方を捕まえる)。
    msg = str(ei.value)
    assert "trek" not in msg.split("got")[0], msg


def test_write_vocabulary_excludes_trek():
    assert "trek" not in claims.valid_target_kinds()


def test_read_vocabulary_still_recognises_trek():
    """ms-185 より前の記録を読む語彙には残る。"""
    assert "trek" in claims.readable_target_kinds()


def test_read_set_is_exactly_write_set_plus_trek():
    """2 つの語彙の差は trek 1 件だけ。

    差分を名指しで固定するのは、将来どちらかに別の種別を足したとき
    「読みにだけ在る」種別が黙って増えるのを防ぐため。
    """
    assert claims.readable_target_kinds() - claims.valid_target_kinds() == frozenset({"trek"})


def test_pre_ms185_trek_claim_still_parses():
    """過去に記録された trek claim が None に落ちない (= 黙って読めなくならない)。"""
    event = {
        "channel": claims.CLAIM_CHANNEL,
        "payload": {
            "kind": claims.PAYLOAD_KIND_CLAIM,
            "claim_kind": "claim",
            "claim_id": "cl-1750000000-abc",
            "target": {"kind": "trek", "id": "tk-abc123"},
            "from_session_id": "sv-old",
        },
    }
    parsed = claims.parse_claim_event(event)
    assert parsed is not None, "pre-ms-185 の trek claim が読めなくなっている"
    assert parsed["target"]["kind"] == "trek"


def test_help_surfaces_list_exactly_the_write_vocabulary():
    """利用者向けの案内が、作成できる種別の集合と**一致**すること。

    初版は「trek が無いこと」だけを測っていた。それだと次に別の種別を足す / 外す人が
    集合だけ直して緑のまま終われ、案内 3 面は古いまま残る (独立レビュー 3 周目の指摘)。
    そこで測るのは一致にする —— trek の不在はその帰結として付いてくる。

    `lib/cmd_claim.py` のエラー文は集合から導出するようにしたのでこの一覧から外した
    (= 直す面を 1 つ減らした)。残る 3 面は散文なので機械で生成できず、突合で守る。
    """
    expected = set(claims.valid_target_kinds())
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # `bin/beacon` は "target kind:" の行を **2 つ** 持つ。もう 1 つは
    # `beacon stop scoped` のもので語彙が違う (ms | task | session — 正本は
    # stop_signal._VALID_TARGET_KINDS)。素朴に最初の一致を取ると別コマンドの行を
    # claim の語彙と比べて偽の赤になる (初版で実際にそうなった)。claim の usage
    # ブロックに入ってから探す。
    for rel, needle, after in (
        ("README.md", "beacon claim request", None),
        ("bin/beacon", "target kind:", "beacon claim view"),
        ("lib/commands.py", "Announce intent to take a target", None),
    ):
        path = os.path.join(root, rel)
        found = None
        armed = after is None
        with open(path, encoding="utf-8") as f:
            for line in f:
                if not armed:
                    armed = after in line
                    continue
                if needle not in line:
                    continue
                # 括弧 / コロンのあとに並ぶ種別名を拾う (区切りは `/` `|` `,`)。
                kinds = set(re.findall(
                    r"\b(" + "|".join(re.escape(k) for k in claims.readable_target_kinds())
                    + r")\b(?!-\d)", line))
                if kinds:
                    found = kinds
                    break
        assert found is not None, f"{rel}: 案内の行 ({needle!r}) が見つからない"
        assert found == expected, (
            f"{rel} の案内が {sorted(found)} を挙げていますが、作成できる種別は "
            f"{sorted(expected)} です"
        )


def test_cmd_claim_error_text_is_derived_not_hand_written():
    """エラー文が集合から組み立てられていること (= 手書きの面を増やさない)。"""
    import inspect
    import sys as _sys
    _sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
    import cmd_claim  # noqa: PLC0415
    src = inspect.getsource(cmd_claim)
    assert "valid_target_kinds()" in src, (
        "cmd_claim のエラー文が集合から導出されていません (手書きに戻っている)"
    )
    assert "kind = ms|task|operation|free" not in src, "語彙が手書きで戻っている"
