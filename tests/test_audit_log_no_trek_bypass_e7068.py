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

T1-system 署名による bypass (= ms-83 / e-1995、サーバ自身が発行した署名 = 規則 0)
は **別の権限**で、この file が書かれた時点では生きていた。その去就を e-7078 で
決め、**消す**ことにした (発行側が Trek の active を要求していたため発行者が
恒久に 0 になり、迂回する側の分岐だけが残っていた / 本番の自律発火は人間が
承認した T2 封筒を使うので規則 0 に依存していない)。

いまこの file が測るのは **2 つの退役がどちらも空振りでないこと**:
共有 Trek 規則 (e-7068) と 規則 0 (e-7078) の両方について、理由コードも
駆動する引数も残っておらず、かつて省略されていた組み合わせが gate される。
判断記録 dec-4e7a66391176699a。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import dm_gate  # noqa: E402

APP_PY = os.path.join(os.path.dirname(__file__), "..", "server", "app.py")


def _audit_dm_gate_field(sender_uid, receiver_uid, actions):
    """``server/app.py`` の ``audit_record["dm_gate"]`` 組み立てを再現する。

    ms-185 e-7078 / e-7323: 以前はここに ``tier`` / ``issuer`` を渡す口が在った
    (= 規則 0 を駆動するため)。規則 0 ごと落としたので、本番の呼び出しと同じく
    3 つの事実だけを渡す。
    """
    should_gate, gate_reason = dm_gate.should_gate_dm_action(
        sender_user_id=sender_uid,
        receiver_user_id=receiver_uid,
        actions_authorized=actions,
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


def test_the_t1_system_bypass_is_retired_not_vacuous():
    """規則 0 (= サーバ署名による確認の省略) が **判定ごと** 無いことを測る。

    **この試験が置き換えたもの**: ここには ``test_t1_system_authority_is_untouched``
    が在り、「サーバ自身が発行した署名による bypass は生きている / こちらを一緒に
    消すと本番の自律発火が止まる / 去就は e-7078 で決める」と書いていた。

    **その前提は #812 以後は誤り**だった。実測 (e-7078): 本番の Operation 発火
    ``_fire_operation`` は ``beacon operation approve`` で人間が発行した **T2** 封筒を
    再利用しており、T1-system を発行しない。送受信は同一ユーザーなので規則 1
    (same_user) で通る。つまり規則 0 を落としても本番の自律発火は止まらない。
    一方で発行側は Trek が active であることを要求していたので、Trek を畳んだ
    時点で発行者が恒久に 0 になり、**迂回する側の分岐だけが残っていた**。

    **だから「残す」ではなく「消す」を選び、pin を空振りにせず裏返した。**
    対象を消して古い pin をそのまま緑で通すのは e-7320 で踏んだ型 (= 自分の
    コメントで pin が素通りする偽の緑) なので、同じ轍は踏まない。
    判断記録 dec-4e7a66391176699a。

    測るのは 3 つ: 理由コードが無い / 判定を駆動する引数が無い / 同じ組み合わせが
    いま gate される (= 削除の向きが fail-closed)。
    """
    assert not hasattr(dm_gate, "GATE_REASON_T1_SYSTEM"), (
        "規則 0 の理由コードが残っている — 誰も返さない理由コードは、"
        "読み手に「その経路が在る」と誤読させる")

    import inspect
    params = set(inspect.signature(dm_gate.should_gate_dm_action).parameters)
    assert "envelope_tier" not in params, (
        "規則 0 が唯一の読み手だった引数が残っている (誰も使わない引数を作らない)")
    assert "envelope_issuer" not in params

    # かつて迂回していた組み合わせ (別人 + action 付き + サーバ署名を名乗る) が、
    # いまは通常の cross-user 判定に落ちる。
    audit = _audit_dm_gate_field(
        "uid-system", "uid-alice", ["operation.check"])
    assert audit["should_gate"] is True
    assert audit["reason"] == dm_gate.GATE_REASON_CROSS_USER_ACTION
