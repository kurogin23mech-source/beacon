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


def test_help_surfaces_do_not_advertise_trek():
    """利用者向けの案内 4 面が trek を取れる種別として挙げていないこと。

    集合を直しても案内が古いままなら、利用者は通らない呼び出しを教わる。
    実ファイルを読むのは、案内がコードの定数から生成されていないため
    (= 二重フロントなので両方を見ないと片方だけ古くなる)。
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for rel, needle in (
        ("README.md", "beacon claim request"),
        ("lib/cmd_claim.py", "kind = ms"),
        ("bin/beacon", "target kind: ms"),
        ("lib/commands.py", "Announce intent to take a target"),
    ):
        with open(os.path.join(root, rel), encoding="utf-8") as f:
            for line in f:
                if needle in line:
                    assert "trek" not in line, f"{rel}: {line.strip()}"
