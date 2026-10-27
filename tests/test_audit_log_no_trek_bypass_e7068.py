"""監査記録から Trek の bypass 痕跡が消えたことを測る (ms-185 e-7068)。

**このファイルが置き換えたもの**: ``tests/test_audit_log_trek_id.py`` (AC19) は、
「共有 Trek による確認省略が成立したとき、監査記録に ``bypass=True`` と
``trek_id=<根拠の trek>`` が載る」ことを 258 行かけて固定していた。ms-185 で
その規則を**判定ごと削除**したので、固定すべき対象が無くなった。元ファイルは
``.trash/ms-185-e7068/`` に退避してある (消さずに残す)。

**なぜ空にせず裏返しを置くのか**: 規則を消した痕跡として、以下 2 つは測る価値が
ある。

  1. 監査記録に **常に空の field が残っていない**こと。``bypass`` / ``trek_id``
     を「常に False / 常に空」で残すのが一番ありがちな後退で、読み手は
     「bypass=False だから Trek では通っていない」と読む一方、実際には
     *判定が存在しない*。意味の無い field は、無い方が正直。
  2. かつて bypass していた組み合わせが、いま **gate される**こと。削除の向きが
     fail-closed であることの証拠。

T1-system 署名による bypass (= ms-83 / e-1995、サーバ自身が発行した署名) は
**別の権限**で、こちらは生きている。混同しないことも併せて測る (その去就は
e-7078 で決める)。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import dm_gate  # noqa: E402

APP_PY = os.path.join(os.path.dirname(__file__), "..", "server", "app.py")


def _audit_dm_gate_field(sender_uid, receiver_uid, actions,
                         tier="", issuer=""):
    """``server/app.py`` の ``audit_record["dm_gate"]`` 組み立てを再現する。"""
    should_gate, gate_reason = dm_gate.should_gate_dm_action(
        sender_user_id=sender_uid,
        receiver_user_id=receiver_uid,
        actions_authorized=actions,
        envelope_tier=tier,
        envelope_issuer=issuer,
    )
    return {
        "should_gate": should_gate,
        "reason": gate_reason,
        "sender_user_id": sender_uid,
        "receiver_user_id": receiver_uid,
    }


def test_audit_record_carries_no_always_empty_trek_fields():
    """監査記録に ``bypass`` / ``trek_id`` が残っていない。

    常に False / 常に空の field を残すのが一番ありがちな後退。読み手は
    「bypass=False」を「Trek 経由では通っていない」と読むが、実際には判定が
    存在しない — 意味の無い field は無い方が正直。
    """
    audit = _audit_dm_gate_field("uid-alice", "uid-bob", ["commit"])
    assert "bypass" not in audit
    assert "trek_id" not in audit
    assert set(audit) == {"should_gate", "reason",
                          "sender_user_id", "receiver_user_id"}


def test_the_server_no_longer_stamps_a_trek_id_into_the_audit_record():
    """本番側の組み立てにも ``trek_id`` が残っていないことを実ファイルで見る。

    上の再現関数は**テストが書いた写し**なので、それだけでは本番が残していても
    気付けない (写しと本番がズレる = ms-185 で繰り返し踏んだ型)。実ファイルの
    該当 block を読んで確認する。
    """
    with open(APP_PY, encoding="utf-8") as fh:
        src = fh.read()
    i = src.index('audit_record["dm_gate"] = {')
    block = src[i:src.index("}", i)]
    assert "trek_id" not in block, (
        "server/app.py の監査記録に trek_id が残っている — 判定が無いのに "
        "常に空の field を刻んでいる形")
    assert "bypass" not in block


def test_the_pair_that_used_to_bypass_is_now_gated():
    """かつて bypass していた組み合わせ (別人 + action 付き) が gate される。"""
    audit = _audit_dm_gate_field("uid-alice", "uid-bob", ["commit"])
    assert audit["should_gate"] is True
    assert audit["reason"] == dm_gate.GATE_REASON_CROSS_USER_ACTION


def test_t1_system_authority_is_untouched():
    """サーバ自身が発行した署名による bypass は別の権限で、生きている。

    こちらを一緒に消すと本番の自律発火が止まる。去就は e-7078 で決める。
    """
    audit = _audit_dm_gate_field(
        "uid-system", "uid-alice", ["operation.check"],
        tier="T1-system", issuer="beacon-system")
    assert audit["should_gate"] is False
    assert audit["reason"] == dm_gate.GATE_REASON_T1_SYSTEM
