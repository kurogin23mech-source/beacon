"""共有 Trek による確認省略を「消したこと」のガードが、戻したときに本当に赤くなるか
を 1 件ずつ測る反転ハーネス (ms-185 e-7068)。

**消したものを守るガードは、戻す方向に壊して測るしかない。** 足したガードは
「壊したら赤くなるか」を測れば済むが、削除は「復活させたら赤くなるか」でしか
証明できない。復活のさせ方は 1 つではない (判定を戻す / 定数だけ戻す / 常に偽の
引数として残す / 監査に空の field を刻む / 別の層で救済する) ので、**ありがちな
戻し方を列挙して全部測る**。1 つでも素通りするなら、その形で後退できる。

逆方向も測る: 一緒に消してはいけない権限 (同一ユーザー / サーバ自身の署名) を
消したときに赤くなるか。削除作業は「消しすぎ」でも壊れる。

バイトコードキャッシュの隔離と実行後のベースライン再実測は
``scripts/invert_harness.py`` が持つ。

使い方: python3 scripts/invert-shared-trek-rule-removal.py
終了コード: 全ケースで期待どおり赤くなれば 0、1 件でも素通りなら 1。
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import invert_harness as ih  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
GATE = ROOT / "server" / "dm_gate.py"
APP = ROOT / "server" / "app.py"
CONSENT = ROOT / "lib" / "dm_consent.py"
# ms-185 e-7080 / e-7321: ここに CMD_BUS (= lib/cmd_bus.py) を変異対象として
# 持っていた。送信予算の迂回判定を判定ごと撤去したので当て先が無くなり、
# 変異 2 件と一緒に落とした。CMD_BUS_TEST は baseline の一員として残す
# (予算ゲート本体の試験はいまも在り、壊れていないことを毎周確認する)。
# ms-185 e-7078: 規則 0 の削除は dm_gate だけでなく tier 自体 (envelope) と
# 送信側 carve-out (app) にも跨るので、3 ファイルを変異対象に持つ。
ENVELOPE = ROOT / "server" / "envelope.py"

GATE_TEST = "tests/test_dm_gate.py"
AUDIT_TEST = "tests/test_audit_log_no_trek_bypass_e7068.py"
CONSENT_TEST = "tests/test_dm_consent.py"
BACKSTOP_TEST = "tests/test_sender_consent_backstop.py"
ALIGN_TEST = "tests/test_dm_consent_skill_alignment_e6349.py"
# ms-185 e-7070: 旧 GRAIN_TEST (tests/test_dm_gate_session_grain.py) と
# 旧 PARITY_TEST (tests/test_trek_member_rule_parity_e7069.py) は、どちらも
# 消えた正本 (lib/trek.py の在籍判定) を駆動していたので本体と一緒に落ちた。
# 「在籍が証明できても判定器に届かない」という e-7068 の主張は GATE_TEST 側の
# 2 件 (test_shared_trek_no_longer_bypasses_the_gate /
# test_the_judge_has_no_trek_shaped_input_or_output) が被覆しており、
# dm_gate の不在検査 1 件は tests/test_dm_gate.py へ移設してある。
# CLI 側を壊す 2 件は、緩む方へ転んだことを CLI 側自身の試験で受ける。
CMD_BUS_TEST = "tests/test_bus_budget.py"
ENVELOPE_TEST = "tests/test_envelope.py"
# 鍵を消すと「本番で未設定なら起動を拒む」起動時ガードが成立しなくなる。
# 鍵の生存を測るなら、起動ガード (tests/test_scheduler_key_startup.py) ではなく
# **残った唯一の消費者** を当て先にする。起動ガードは env 変数を操作して
# 「本番で未設定なら起動を拒む」を測るので、鍵を取る関数が消えても赤くならない
# (実測: 鍵を消しても 12 passed)。ティック endpoint の試験は鍵で認証するため
# 6 件が赤くなる。鍵を残す理由そのもの (= 本番のティック駆動) を測る形。
SCHEDULER_KEY_TEST = ("tests/test_system_tick_entrypoint_e7065.py"
                      "::test_new_entrypoint_requires_the_internal_key")
BASELINE = [GATE_TEST, AUDIT_TEST, CONSENT_TEST, BACKSTOP_TEST, ALIGN_TEST,
            CMD_BUS_TEST, ENVELOPE_TEST,
            "tests/test_system_tick_entrypoint_e7065.py"]


# --- 戻す方向 (= 規則の復活) ------------------------------------------------

def gate_restores_the_rule(src: str) -> str:
    """判定を丸ごと戻す (= 引数 + 理由コード + Rule 2)。"""
    anchor = '''GATE_REASON_SAME_USER = "same_user"'''
    assert anchor in src, "anchor not found (gate_restores_the_rule: const)"
    src = src.replace(
        anchor,
        anchor + '\nGATE_REASON_SHARED_TREK = "shared_trek_member"  # MUTANT', 1)
    anchor2 = "    actions_authorized: Iterable[str] | None,\n"
    assert anchor2 in src, "anchor not found (gate_restores_the_rule: param)"
    src = src.replace(
        anchor2,
        anchor2 + "    shared_trek_lookup=None,  # MUTANT\n", 1)
    anchor3 = "    # Cross-user + actions implied → gate triggers."
    assert anchor3 in src, "anchor not found (gate_restores_the_rule: rule)"
    return src.replace(
        anchor3,
        "    if shared_trek_lookup is not None and shared_trek_lookup(  # MUTANT\n"
        "            sender_user_id, receiver_user_id):\n"
        "        return (False, GATE_REASON_SHARED_TREK)\n"
        + anchor3, 1)


def gate_bypasses_cross_user_unconditionally(src: str) -> str:
    """規則を「引数なしで効く既定」として戻す (= 一番危険な戻し方)。

    上の ``gate_restores_the_rule`` は引数経由で戻す形で、**振る舞いの試験では
    捕まらない** (引数を渡さない呼び方では規則が発火しないため)。そちらは構造の
    試験 (引数も理由コードも無いこと) が捕まえる。こちらは逆に、呼び出し側が
    何も渡さなくても確認を省く形 — 振る舞いの試験が捕まえなければならない。
    """
    anchor = "    # Cross-user + actions implied → gate triggers."
    assert anchor in src, "anchor not found (gate_bypasses_cross_user_unconditionally)"
    return src.replace(
        anchor,
        '    return (False, "shared_trek_member")  # MUTANT\n' + anchor, 1)


def gate_restores_only_the_constant(src: str) -> str:
    """理由コードだけ戻す (= 誰もセットしない定数が 1 つ生える)。"""
    anchor = '''GATE_REASON_SAME_USER = "same_user"'''
    assert anchor in src, "anchor not found (gate_restores_only_the_constant)"
    return src.replace(
        anchor,
        anchor + '\nGATE_REASON_SHARED_TREK = "shared_trek_member"  # MUTANT', 1)


def gate_keeps_a_vestigial_param(src: str) -> str:
    """使われない引数だけ残す (= 「とりあえず互換のため」で一番起きやすい形)。"""
    anchor = "    actions_authorized: Iterable[str] | None,\n"
    assert anchor in src, "anchor not found (gate_keeps_a_vestigial_param)"
    return src.replace(
        anchor, anchor + "    shared_trek_lookup=None,  # MUTANT\n", 1)


def gate_keeps_the_session_grain_params(src: str) -> str:
    """session id の引数だけ残す (= 共有 Trek の session 粒度判定の残骸)。

    ms-185 e-7078: 当て先は ``envelope_issuer`` 行だった。その引数は規則 0 と
    一緒に消えたので、**署名の最後の引数行へ張り替えた**。アンカーが外れると
    この変異は SKIP になり、ハーネスは「SKIP は緑と同じ害」と言いながら通って
    しまう — 削除作業で anchor を消したら、同じ変更で張り替える。
    """
    anchor = "    actions_authorized: Iterable[str] | None,\n"
    assert anchor in src, "anchor not found (gate_keeps_the_session_grain_params)"
    return src.replace(
        anchor,
        anchor + "    sender_session_id: str = \"\",  # MUTANT\n"
                 "    receiver_session_id: str = \"\",  # MUTANT\n", 1)


def app_restamps_the_audit_fields(src: str) -> str:
    """監査記録に常に空の bypass / trek_id を戻す。"""
    anchor = '''        "sender_user_id": authoritative_sender_uid,
        "receiver_user_id": receiver_uid,
    }'''
    assert anchor in src, "anchor not found (app_restamps_the_audit_fields)"
    return src.replace(
        anchor,
        '''        "sender_user_id": authoritative_sender_uid,
        "receiver_user_id": receiver_uid,
        "bypass": False,  # MUTANT
        "trek_id": "",  # MUTANT
    }''', 1)


def app_restores_the_last_resort_rescue(src: str) -> str:
    """拒否する前の「最後の救済」を戻す (= 送信側の層で通してしまう形)。

    **層が 2 つあることに注意**: 受信側の保留 (``dm_gate``) と送信側の拒否
    (``dm_consent`` + server の 403) は別の層で、片方を壊してももう片方は効く。
    HTTP で 403 になるかを測る試験は**送信側の層**を見ているので、受信側の判定を
    戻しても赤くならない (実測済 — 最初この組み合わせで素通りした)。この変異は
    送信側を戻す。
    """
    anchor = "            # ms-185 e-7068: ここに「拒否する前の最後の救済"
    assert anchor in src, "anchor not found (app_restores_the_last_resort_rescue)"
    return src.replace(
        anchor, "            consent_allow = True  # MUTANT\n" + anchor, 1)

def consent_restores_the_carve_out(src: str) -> str:
    """送信側の確認免除に共有 Trek を戻す。"""
    anchor = '''    operation_envelope: bool = False,
) -> tuple[bool, str]:'''
    assert anchor in src, "anchor not found (consent_restores_the_carve_out: param)"
    src = src.replace(
        anchor,
        '''    operation_envelope: bool = False,
    shared_trek: bool = False,  # MUTANT
) -> tuple[bool, str]:''', 1)
    anchor2 = "    # Rule 6 (e-3492 P1 fix): only a PROVEN cross-user pair may require"
    assert anchor2 in src, "anchor not found (consent_restores_the_carve_out: rule)"
    return src.replace(
        anchor2,
        '    if shared_trek:  # MUTANT\n'
        '        return (False, "shared_trek_member")\n\n'
        + anchor2, 1)


def consent_mixes_retired_into_the_live_map(src: str) -> str:
    """引退した判定理由を生きている辞書に戻す (= 現役の規則に見える)。"""
    anchor = '''    CONSENT_REQUIRED_CROSS_USER:'''
    assert anchor in src, "anchor not found (consent_mixes_retired_into_the_live_map)"
    return src.replace(
        anchor,
        '    "shared_trek_member": "MUTANT",\n' + anchor, 1)


# --- 消しすぎる方向 (= 一緒に消してはいけない権限) --------------------------

# --- 規則 0 (サーバ署名による確認省略) を戻す方向 (ms-185 e-7078 / e-7323) ----
#
# e-7068 の時点では、この権限は「一緒に消してはいけないもの」として逆方向に
# 測っていた。e-7078 で去就を決め、**消す**ことにしたので、測る向きが反転した
# (= いまは戻ってこないことを測る)。発行者が恒久に 0 になった tier の判定を
# 残すと、迂回する側の分岐だけが生き続ける形になるため。

def gate_restores_rule_zero_entirely(src: str) -> str:
    """規則 0 を丸ごと戻す (= 判定も理由コードも引数も)。"""
    anchor = "    # Rule 1: same user → always skip."
    assert anchor in src, "anchor not found (gate_restores_rule_zero_entirely)"
    restored = (
        '    GATE_REASON_T1_SYSTEM = "t1_system_envelope"  # MUTANT\n'
        '    if envelope.get("tier") == "T1-system":  # MUTANT\n'
        '        return (False, GATE_REASON_T1_SYSTEM)\n\n'
    )
    return src.replace(anchor, restored + anchor, 1)


def gate_restores_only_the_t1_system_reason_code(src: str) -> str:
    """理由コードだけ戻す (= 誰も返さない理由コードが語彙に残る)。"""
    anchor = 'GATE_REASON_CROSS_USER_ACTION = "cross_user_action_pending_approval"'
    assert anchor in src, "anchor not found (gate_restores_only_the_t1_system_reason_code)"
    return src.replace(
        anchor,
        anchor + '\nGATE_REASON_T1_SYSTEM = "t1_system_envelope"  # MUTANT', 1)


def gate_keeps_the_envelope_tier_param(src: str) -> str:
    """規則 0 を駆動していた引数だけ残す (= 誰も読まない引数)。"""
    anchor = "    actions_authorized: Iterable[str] | None,\n) -> tuple[bool, str]:"
    assert anchor in src, "anchor not found (gate_keeps_the_envelope_tier_param)"
    return src.replace(
        anchor,
        '    actions_authorized: Iterable[str] | None,\n'
        '    envelope_tier: str = "",  # MUTANT\n'
        '    envelope_issuer: str = "",  # MUTANT\n'
        ") -> tuple[bool, str]:", 1)


def envelope_restores_the_retired_tier(src: str) -> str:
    """消した tier を語彙に戻す (= 偽造封筒が verify を通れる形に戻る)。"""
    anchor = 'VALID_TIERS = {TIER_T1, TIER_T2, TIER_T3, TIER_T5}'
    assert anchor in src, "anchor not found (envelope_restores_the_retired_tier)"
    return src.replace(
        anchor,
        'TIER_T1_SYSTEM = "T1-system"  # MUTANT\n'
        'T1_SYSTEM_ISSUER = "beacon-system"  # MUTANT\n'
        'VALID_TIERS = {TIER_T1, TIER_T1_SYSTEM, TIER_T2, TIER_T3, TIER_T5}', 1)


def app_restores_the_retired_tier_carve_out(src: str) -> str:
    """送信側の事前承認 carve-out に消した tier を戻す。"""
    anchor = "        consent_operation_env = env_tier == envelope_mod.TIER_T2"
    assert anchor in src, "anchor not found (app_restores_the_retired_tier_carve_out)"
    return src.replace(
        anchor,
        '        consent_operation_env = env_tier in (  # MUTANT\n'
        '            "T1-system", envelope_mod.TIER_T2,\n'
        '        )', 1)


# --- 消しすぎる方向 (= 一緒に消してはいけないもの) --------------------------
#
# ms-185 e-7078: 発行口を落とすときに **スケジューラ鍵まで一緒に消す** のが
# 新しい消しすぎリスク。この鍵は T1-system 発行口専用ではなく
# POST /api/system/tick も守っており、本番のティック駆動 (scripts/vps-tick.sh)
# がそれを使う。消すと本番の Operation 自律が止まる。

def envelope_drops_the_scheduler_key(src: str) -> str:
    """スケジューラ鍵を発行口と一緒に消す (= 本番のティック駆動が止まる)。"""
    anchor = "def scheduler_internal_key() -> str:"
    assert anchor in src, "anchor not found (envelope_drops_the_scheduler_key)"
    return src.replace(
        anchor,
        "def _removed_scheduler_internal_key() -> str:  # MUTANT", 1)


def gate_drops_the_same_user_rule(src: str) -> str:
    """同一ユーザーの自己宛を一緒に消す (= 自分のセッション同士が止まる)。"""
    anchor = '''    if sender_user_id and sender_user_id == receiver_user_id:
        return (False, GATE_REASON_SAME_USER)'''
    assert anchor in src, "anchor not found (gate_drops_the_same_user_rule)"
    return src.replace(anchor, "    pass  # MUTANT", 1)


# --- 2 実装のズレ (e-7069: 正本 lib/trek.py ↔ CLI 側 lib/cmd_bus.py) --------
#
# 実装を 1 本に寄せず「黙ってズレない」ことだけを固定したので、ズレを作る
# 変異で赤くなることが、その検査が本物である唯一の根拠になる。**両側**を
# 別々に壊す: どちらを直し忘れても捕まる形でなければ意味がない。


# ms-185 e-7070: trek_phase_a_needs_only_the_sender を落とした。正本
# (lib/trek.py の在籍判定) を壊す変異で、その正本ごと消えた。
#
# ms-185 e-7080 / e-7321: CLI 側の粒度を壊す 2 件 (session 粒度を緩める /
# pre-A が何でも一致する) も落とした。測っていた対象 = 送信予算の迂回判定
# (_is_trek_internal_send とそれが使う参加者の粒度判定) を判定ごと撤去したので、
# 当てる場所が無い。**判定が消えたら、その判定のための変異も消す** —
# アンカーの無い変異を残すと SKIP になり、ハーネス自身が「SKIP は緑と同じ害」と
# 言いながら素通りする (この MS で実際に 1 件踏んだ)。
#
# 迂回が戻ってこないことは、迂回路を足すと赤くなる形では測っていない。
# 予算ゲートが**例外なく通る**ことは tests/test_bus_budget.py 前半の
# 予算ゲート本体の試験が測っており、迂回路を足す変異は「新しい迂回路の形」を
# 先に決めないと書けない (= 存在しない形への変異は書けない)。


CASES = [
    # 引数経由で戻す形は、引数を渡さない呼び方では発火しないので**振る舞いでは
    # 捕まらない**。構造の試験 (引数も理由コードも無いこと) が唯一の防御になる。
    ("判定を丸ごと戻す (引数経由)", GATE, gate_restores_the_rule,
     GATE_TEST + "::test_the_judge_has_no_trek_shaped_input_or_output"),
    # 引数なしで効く既定として戻す形は、振る舞いの試験が捕まえなければならない。
    ("確認省略を既定にする (引数なしで効く)", GATE,
     gate_bypasses_cross_user_unconditionally,
     GATE_TEST + "::test_shared_trek_no_longer_bypasses_the_gate"),
    ("確認省略を既定にする (在籍と確認省略の切り離し)", GATE,
     gate_bypasses_cross_user_unconditionally,
     GATE_TEST + "::test_shared_trek_no_longer_bypasses_the_gate"),
    ("拒否する前の最後の救済を戻す (HTTP 経路)", APP,
     app_restores_the_last_resort_rescue,
     BACKSTOP_TEST + "::test_shared_trek_cross_user_is_now_rejected_without_claim"),
    ("理由コードだけ戻す", GATE, gate_restores_only_the_constant,
     GATE_TEST + "::test_the_judge_has_no_trek_shaped_input_or_output"),
    ("使われない引数だけ残す", GATE, gate_keeps_a_vestigial_param,
     GATE_TEST + "::test_the_judge_has_no_trek_shaped_input_or_output"),
    ("session id の引数だけ残す", GATE, gate_keeps_the_session_grain_params,
     GATE_TEST + "::test_the_judge_has_no_trek_shaped_input_or_output"),
    ("監査に常に空の field を戻す", APP, app_restamps_the_audit_fields,
     AUDIT_TEST + "::test_the_server_no_longer_stamps_a_trek_id_into_the_audit_record"),
    ("送信側の確認免除を戻す", CONSENT, consent_restores_the_carve_out,
     CONSENT_TEST + "::test_shared_trek_is_no_longer_a_carve_out"),
    ("引退した理由を現役の辞書に戻す", CONSENT, consent_mixes_retired_into_the_live_map,
     ALIGN_TEST + "::test_retired_reasons_live_in_their_own_map_not_the_live_one"),
    # 規則 0 を戻す方向 (ms-185 e-7078 / e-7323)
    ("規則 0 を丸ごと戻す", GATE, gate_restores_rule_zero_entirely,
     AUDIT_TEST + "::test_the_t1_system_bypass_is_retired_not_vacuous"),
    ("規則 0 の理由コードだけ戻す", GATE,
     gate_restores_only_the_t1_system_reason_code,
     GATE_TEST + "::test_the_judge_has_no_trek_shaped_input_or_output"),
    ("規則 0 の引数だけ残す", GATE, gate_keeps_the_envelope_tier_param,
     GATE_TEST + "::test_the_judge_has_no_trek_shaped_input_or_output"),
    ("消した tier を語彙に戻す", ENVELOPE, envelope_restores_the_retired_tier,
     ENVELOPE_TEST + "::test_the_retired_tier_cannot_be_issued"),
    ("送信側の carve-out に消した tier を戻す", APP,
     app_restores_the_retired_tier_carve_out,
     BACKSTOP_TEST + "::test_the_retired_server_signature_is_no_longer_a_carve_out"),
    # 消しすぎる方向
    ("スケジューラ鍵を一緒に消す", ENVELOPE, envelope_drops_the_scheduler_key,
     SCHEDULER_KEY_TEST),
    ("同一ユーザーの規則を一緒に消す", GATE, gate_drops_the_same_user_rule,
     GATE_TEST + "::test_gate_skip_when_sender_equals_receiver"),
    # 2 実装のズレ (e-7069) — 正本側と CLI 側を別々に壊す
    # ms-185 e-7070: 当て先は CLI 側だけになった (正本が消えて実装が 1 本)。
    # 受け手も「正本より緩くないか」の相対比較から、粒度そのものを測る絶対の
    # 試験へ移設してある (tests/test_bus_budget.py の末尾)。
]

# 赤いこと自体はガードが効いた証拠にならない (変異が構文を壊しても赤くなる)。
# 赤の理由の判定は ``invert_harness.classify_pytest_result`` が 1 箇所で持つ。
def run_one(target: str):
    return ih.run_and_classify(target, ROOT)


def main() -> int:
    originals = {p: p.read_text(encoding="utf-8")
                 for p in (GATE, APP, CONSENT, ENVELOPE)}
    if not ih.check_baseline(BASELINE, ROOT):
        return 1

    bad = 0
    for label, path, mutate, target in CASES:
        try:
            path.write_text(mutate(originals[path]), encoding="utf-8")
            ih.invalidate_bytecode_cache(path, ROOT)
            went_red, tail = run_one(target)
        except AssertionError as exc:
            ih.restore(originals, ROOT)
            print(f"  SKIP  {label}: 当てる場所が無い ({exc}) — SKIP は緑と同じ害")
            bad += 1
            continue
        finally:
            ih.restore(originals, ROOT)
        print(("  OK  " if went_red else " MISS ") + label)
        print(f"          → {target}: {tail}")
        if not went_red:
            bad += 1

    print()
    if bad:
        print(f"NG: {bad} 件が「戻したのに赤くならない」か SKIP。偽の安全なので直すこと。")
        return 1
    if not ih.check_not_polluted(BASELINE, ROOT):
        return 1
    print()
    print(f"OK: {len(CASES)} 件すべて、守る対象を壊すと赤くなった。")
    return 0


if __name__ == "__main__":
    sys.exit(ih.guarded_main(main, sys.argv, __doc__, ROOT))
