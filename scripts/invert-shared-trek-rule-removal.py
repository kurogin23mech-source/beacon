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
# e-7069 (独立保守性レビューの medium): 同じ「共有作業部屋の参加者か」判定が
# 正本 (lib/trek.py) と CLI 側の独自実装 (lib/cmd_bus.py) の 2 箇所にある。
# 突き合わせる検査を足したので、**両方の側を別々に壊して**赤くなるか測る。
# 片側だけ壊して測ると、もう一方が無防備なまま「全件 OK」になる。
TREK = ROOT / "lib" / "trek.py"
CMD_BUS = ROOT / "lib" / "cmd_bus.py"

GATE_TEST = "tests/test_dm_gate.py"
AUDIT_TEST = "tests/test_audit_log_no_trek_bypass_e7068.py"
CONSENT_TEST = "tests/test_dm_consent.py"
BACKSTOP_TEST = "tests/test_sender_consent_backstop.py"
ALIGN_TEST = "tests/test_dm_consent_skill_alignment_e6349.py"
GRAIN_TEST = "tests/test_dm_gate_session_grain.py"
PARITY_TEST = "tests/test_trek_member_rule_parity_e7069.py"
BASELINE = [GATE_TEST, AUDIT_TEST, CONSENT_TEST, BACKSTOP_TEST, ALIGN_TEST,
            GRAIN_TEST, PARITY_TEST]


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
    """session id の引数だけ残す (= 共有 Trek の session 粒度判定の残骸)。"""
    anchor = "    envelope_issuer: str = \"\",\n"
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

def gate_drops_the_server_signature_authority(src: str) -> str:
    """サーバ自身の署名による通過を一緒に消す (= 本番の自律発火が止まる)。"""
    anchor = '''    if envelope_tier == "T1-system" and envelope_issuer == "beacon-system":
        return (False, GATE_REASON_T1_SYSTEM)'''
    assert anchor in src, "anchor not found (gate_drops_the_server_signature_authority)"
    return src.replace(anchor, "    pass  # MUTANT", 1)


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


def cmd_bus_loosens_the_session_grain(src: str) -> str:
    """CLI 側の phase-A 判定を user_id 一致に緩める (= 粒度のズレ)。"""
    anchor = '        return any((m.get("session_id") or "") == session_id for m in members)'
    assert anchor in src, "anchor not found (cmd_bus_loosens_the_session_grain)"
    return src.replace(
        anchor,
        '        return any((m.get("user_id") or "") == user_id for m in members)  # MUTANT',
        1)


def cmd_bus_pre_a_matches_anything(src: str) -> str:
    """CLI 側の pre-A 判定を常に真にする (= 正本より緩い側へ転ぶ)。"""
    anchor = ('    return bool(user_id) and any(\n'
              '        (m.get("user_id") or "") == user_id for m in members\n'
              '    )')
    assert anchor in src, "anchor not found (cmd_bus_pre_a_matches_anything)"
    return src.replace(anchor, "    return True  # MUTANT", 1)


def trek_phase_a_needs_only_the_sender(src: str) -> str:
    """正本の phase-A 判定から受け手の在籍要求を落とす (= 正本側のズレ)。"""
    anchor = "                if sender_in and receiver_in:\n                    return (True, trek_id)"
    assert anchor in src, "anchor not found (trek_phase_a_needs_only_the_sender)"
    return src.replace(
        anchor,
        "                if sender_in:  # MUTANT\n                    return (True, trek_id)",
        1)


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
     GRAIN_TEST + "::test_phase_a_membership_no_longer_reaches_the_judge"),
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
    # 消しすぎる方向
    ("サーバ署名の権限を一緒に消す", GATE, gate_drops_the_server_signature_authority,
     AUDIT_TEST + "::test_t1_system_authority_is_untouched"),
    ("同一ユーザーの規則を一緒に消す", GATE, gate_drops_the_same_user_rule,
     GATE_TEST + "::test_gate_skip_when_sender_equals_receiver"),
    # 2 実装のズレ (e-7069) — 正本側と CLI 側を別々に壊す
    ("CLI 側が session 粒度を緩める", CMD_BUS, cmd_bus_loosens_the_session_grain,
     PARITY_TEST + "::test_phase_a_rule_is_identical_in_both_implementations"),
    ("CLI 側の pre-A が何でも一致する (正本より緩い)", CMD_BUS,
     cmd_bus_pre_a_matches_anything,
     PARITY_TEST + "::test_pre_a_cli_is_never_looser_than_the_canonical_rule"),
    ("正本の phase-A が受け手の在籍を要求しない", TREK,
     trek_phase_a_needs_only_the_sender,
     PARITY_TEST + "::test_phase_a_rule_is_identical_in_both_implementations"),
]

_COLLECTION_FAILURE_MARKS = ("found no collectors", "SyntaxError",
                             "errors in", "error in")


def run_one(target: str):
    went_red, tail, r = ih.run_pytest(target, ROOT)
    if any(m in (r.stdout + r.stderr) for m in _COLLECTION_FAILURE_MARKS):
        return (False, f"INVALID (テストを収集できていない): {tail}")
    return (went_red, tail)


def main() -> int:
    originals = {p: p.read_text(encoding="utf-8")
                 for p in (GATE, APP, CONSENT, TREK, CMD_BUS)}
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
    sys.exit(main())
