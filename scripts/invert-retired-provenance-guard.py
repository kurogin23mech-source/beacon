#!/usr/bin/env python3
"""引退した provenance channel のガードが「守る対象を壊したとき実際に赤くなるか」を測る
反転ハーネス (ms-185 e-7077)。

緑のガードは信頼されるので、**偽の安全は無ガードより悪い**。このスクリプトは
``lib/bus_delivery.py`` と消費側 hook に 1 箇所ずつ壊し方を当てて pytest を走らせ、
意図したテストが赤くなることを確認する。

**最重要ケースは「provenance から名前を消すだけ」**。これが赤くならなければ、Trek を
畳む作業 (ms-185) で署名なし自動実行の窓を開けても誰も気づけない — このガードが存在する
唯一の理由がそこにある。

使い方: python3 scripts/invert-retired-provenance-guard.py
終了コード: 全ケースで期待どおり赤くなれば 0、1 件でも「壊したのに緑」か SKIP なら 1
(SKIP は緑と同じ害なので失敗として数える)。対象ファイルは毎ケース必ず元に戻す (finally)。
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import invert_harness as ih  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
BD = ROOT / "lib" / "bus_delivery.py"
CODEX = ROOT / "scripts" / "codex-inbox-hook.py"
CLAUDE = ROOT / "bin" / "beacon-bus-inbox-hook.py"
GUARD_TEST = "tests/test_retired_provenance_channels_e7077.py"
PINNED_TEST = "tests/test_bus_delivery.py"
CONSUMER_TEST = "tests/test_codex_retired_channel_notice_e7077.py"

# バイトコードキャッシュの隔離と、実行後にベースラインが緑へ戻ることの実測は
# ``scripts/invert_harness.py`` が持つ (なぜその形かの経緯もそこに在る)。
# e-7066 以前はこのファイルと invert-tick-guard.py に別々の実装が並んでおり、
# 片方 (tick 側) だけが穴を持ったままだった — 写すのではなく 1 箇所にした理由。
def _invalidate_bytecode_cache(path: pathlib.Path) -> None:
    ih.invalidate_bytecode_cache(path, ROOT)


def _restore(originals: dict) -> None:
    ih.restore(originals, ROOT)


# --- lib/bus_delivery.py への壊し方 -----------------------------------------

def drop_from_both_sets(src: str) -> str:
    """trek-trigger をどちらの集合からも落とす (= 族の名前を取りこぼす)。

    **これが本番の事故そのもの。** 畳む作業で起きる最も自然な間違いで、古い opt-in が
    残っているプロジェクトで署名なし自動実行が通る。

    ms-185 e-7078 時点では trek-* はまだ現役 (SYSTEM) 側に居るので、落とす先はそこ。
    Trek の bus 面を外す e-7080 / e-7070 で引退済みへ移ったら、当てる場所もそちらへ
    移る (どちらの集合から落ちても「族の名前がどちらにも居ない」状態は同じで、被覆の
    テストが赤くなる)。
    """
    anchor = '    "trek-trigger",\n    "trek-progress-check",'
    assert anchor in src, "anchor not found (drop_from_both_sets)"
    return src.replace(anchor, '    "trek-progress-check",', 1)
def retire_without_removing(src: str) -> str:
    """現役と引退済みの両方に入れる (= 判定の順序に依存する曖昧な状態)。"""
    anchor = "RETIRED_PROVENANCE_CHANNELS: frozenset = frozenset()"
    assert anchor in src, "anchor not found (retire_without_removing)"
    return src.replace(
        anchor,
        'RETIRED_PROVENANCE_CHANNELS: frozenset = frozenset({"trek-trigger"})', 1)


def remove_the_retired_check(src: str) -> str:
    """引退の判定そのものを外す (= ガードの本体を無効化)。"""
    anchor = ("    if channel in retired_channels:\n"
              "        return (PROPOSE_TO_AI, AUTO_EXECUTE, "
              "DOWNGRADE_RETIRED_CHANNEL)\n")
    assert anchor in src, "anchor not found (remove_the_retired_check)"
    return src.replace(anchor, "", 1)


def move_retired_check_after_provenance(src: str) -> str:
    """引退の判定を provenance の後ろに置く (= 署名つきの引退 channel が素通りする)。"""
    retired = ("    if channel in retired_channels:\n"
               "        return (PROPOSE_TO_AI, AUTO_EXECUTE, "
               "DOWNGRADE_RETIRED_CHANNEL)\n")
    prov = ("    if channel in provenance_channels and not "
            "has_required_provenance(event):\n"
            "        return (PROPOSE_TO_AI, AUTO_EXECUTE, "
            "DOWNGRADE_NON_SYSTEM_ENVELOPE)\n")
    assert retired + prov in src, "anchor not found (move_retired_check_after_provenance)"
    return src.replace(retired + prov, prov + retired, 1)


def collide_the_reason_tag(src: str) -> str:
    """理由タグを既存のものと同じにする (= 消費側が誤った直し方を案内する)。"""
    anchor = 'DOWNGRADE_RETIRED_CHANNEL = "retired-provenance-channel"'
    assert anchor in src, "anchor not found (collide_the_reason_tag)"
    return src.replace(anchor,
                       'DOWNGRADE_RETIRED_CHANNEL = "non-system-envelope"', 1)


def break_custom_channel_optin(src: str) -> str:
    """独自 channel の opt-in も署名必須にする (= 意図的な設計を壊す / silent scope 縮小)。

    SPEC 初版が書いていた「許可リストに在っても provenance 対象でなければ常に降格」は
    これ。固定テストが守っているので赤くなる。
    """
    anchor = ("    if channel in provenance_channels and not "
              "has_required_provenance(event):")
    assert anchor in src, "anchor not found (break_custom_channel_optin)"
    return src.replace(anchor, "    if not has_required_provenance(event):", 1)


# --- scripts/codex-inbox-hook.py への壊し方 ---------------------------------

def codex_folds_retired_into_allowlist_miss(src: str) -> str:
    """消費側が引退の理由を allowlist-miss に畳む (= 「opt-in しろ」と誤案内する)。

    足す前の 2-way 分岐に戻した状態。理由を足しても消費側が追従していなければ
    「足した」≠「全域に効く」になる。
    """
    anchor = ('        elif retired_tag is not None and reason == retired_tag:\n'
              '            counts["retired"] += 1\n')
    assert anchor in src, "anchor not found (codex_folds_retired_into_allowlist_miss)"
    return src.replace(anchor, "", 1)


def codex_swaps_the_counter_wiring(src: str) -> str:
    """理由 → カウンタの配線だけを逆転させる (**トークンは一切変えない**)。

    **これが初版のテストを素通りした変異そのもの。** 独立レビューが実測し、
    ソース文字列検査だけのテストでは 7/7 緑のままだった。通知文と真因が
    入れ替わる (署名欠落に「古い opt-in を外せ」、引退に「直せません」) のに、
    どのトークンも消えないので文字列検査では捕まえられない。
    """
    old = ('        if reason == bd.DOWNGRADE_NON_SYSTEM_ENVELOPE:\n'
           '            counts["provenance"] += 1\n'
           '        elif retired_tag is not None and reason == retired_tag:\n'
           '            counts["retired"] += 1\n')
    new = ('        if reason == bd.DOWNGRADE_NON_SYSTEM_ENVELOPE:\n'
           '            counts["retired"] += 1\n'
           '        elif retired_tag is not None and reason == retired_tag:\n'
           '            counts["provenance"] += 1\n')
    assert old in src, "anchor not found (codex_swaps_the_counter_wiring)"
    return src.replace(old, new, 1)


def codex_folds_unknown_into_allowlist_miss(src: str) -> str:
    """未知の理由タグを allowlist-miss に畳む (= 将来の理由が黙って誤案内される)。"""
    old = ('        elif not reason:\n'
           '            counts["allowlist"] += 1\n'
           '        else:\n'
           '            counts["other"] += 1\n')
    new = ('        else:\n'
           '            counts["allowlist"] += 1\n'
           '        if False:\n'
           '            counts["other"] += 1\n')
    assert old in src, "anchor not found (codex_folds_unknown_into_allowlist_miss)"
    return src.replace(old, new, 1)


# --- bin/beacon-bus-inbox-hook.py への壊し方 --------------------------------

def claude_reverts_to_the_fixed_message(src: str) -> str:
    """Claude 側を固定文言に戻す (= AX レビューが misleading として挙げた状態)。

    理由を読まず「channel not in bus_auto_execute_channels」を常に出す形。
    retired / non-system-envelope は **channel が許可リストに在る**ときにしか
    起きないので、この文言は事実と正反対で、読んだ AI を真因と逆の回復ループに
    入れる。初版のテストは「代入文がソースに在るか」しか見ておらず、これを
    「Claude 側は追従不要」と誤って裏付けていた。
    """
    old = ('            f" — {_downgrade_advice(ev.get(\'_downgrade_reason\', \'\'), '
           'str(ev.get(\'channel\') or \'\'))}]"')
    new = ('            " — channel not in bus_auto_execute_channels]"')
    assert old in src, "anchor not found (claude_reverts_to_the_fixed_message)"
    return src.replace(old, new, 1)


def swap_the_retired_and_provenance_advice(src: str) -> str:
    """案内文だけを逆転させる (判定条件は触らない)。

    「証拠の封筒が無い」に対して「古い opt-in を外せ」、引退に対して channel ごとの
    要求を案内する状態。分岐の形は残るので、分岐の存在をソースで見るだけでは
    捕まえられない。

    文言の所有者は ``lib/bus_delivery.downgrade_advice`` 1 つなので、壊す先も
    そこ 1 箇所 (以前は 2 つの hook に別コピーが在り、片方ずつ壊す必要があった)。
    """
    prov = '        requirement = PROVENANCE_REQUIREMENTS.get(str(channel or ""))'
    ret = ('        return ("channel は allowlist 済だが、サーバが発行する channel では "\n'
           '                "なくなっています (= 古い opt-in が残った状態)。"\n'
           '                "`beacon bus auto-execute remove --channel <名前>` で外してください")')
    assert prov in src and ret in src, \
        "anchor not found (swap_the_retired_and_provenance_advice)"
    # retired の分岐が、証拠欠落のほうの案内 (channel ごと) を返すようにする
    return src.replace(
        ret,
        '        return downgrade_advice(DOWNGRADE_NON_SYSTEM_ENVELOPE, channel)', 1)


def advice_ignores_the_channel(src: str) -> str:
    """証拠欠落の案内が channel を見ず、trek の世界観の固定文を常に返す形。

    **これが #812 レビューが high で挙げた状態そのもの**: operation-trigger にも
    「operator には直せない」と返し、``beacon operation approve <op-id>`` という
    実在する回復手段を隠す。
    """
    anchor = ('        requirement = PROVENANCE_REQUIREMENTS.get(str(channel or ""))')
    assert anchor in src, "anchor not found (advice_ignores_the_channel)"
    return src.replace(anchor, '        return _TREK_ADVICE\n' + anchor, 1)


def drop_the_advice_from_the_requirement(src: str) -> str:
    """ゲートした channel が回復手段を宣言していない状態に戻す。

    述語だけが在り説明が空 = 「ゲートしているのに operator が打つ手が無い」形。
    構造テストがこれを赤にするはずで、しなければ宣言を束ねた意味が無い。
    """
    anchor = '_OPERATION_ADVICE = ('
    assert anchor in src, "anchor not found (drop_the_advice_from_the_requirement)"
    return src.replace(anchor, '_OPERATION_ADVICE = ""\n_UNUSED_ADVICE = (', 1)


# --- bin/beacon-bus-inbox-hook.py への壊し方 --------------------------------

def claude_keeps_its_own_copy_of_the_advice(src: str) -> str:
    """Claude 側が共有層を呼ばず、自分の固定文を持つ形に戻す。

    欠陥が 2 箇所に在った原因そのもの。両 hook が同じ案内を返すことを測る
    パリティテストが赤くなるはず。
    """
    anchor = '    return bd.downgrade_advice(reason, channel)'
    assert anchor in src, "anchor not found (claude_keeps_its_own_copy_of_the_advice)"
    return src.replace(
        anchor,
        '    if reason == bd.DOWNGRADE_NON_SYSTEM_ENVELOPE:\n'
        '        return ("channel は allowlist 済だが envelope が T1-system でない "\n'
        '                "(= 偽装の恐れ)。opt-in し直しても直りません")\n'
        '    return bd.downgrade_advice(reason, channel)', 1)


def make_a_gated_channel_unsatisfiable(src: str) -> str:
    """永久に満たせない門を足す (= e-7078 が消した状態の再生産)。

    **独立 AX レビュー (high) が隔離コピーで実証した変異**。以前は「述語が宣言されて
    いるか」と「advice が空でないか」しか測っていなかったので、
    ``predicate=lambda ev: False`` + ``advice="x"`` の channel を足しても全件緑だった。
    """
    tail = ('    "trek-leader-digest": ProvenanceRequirement(\n'
            '        has_server_minted_envelope, _TREK_ADVICE),\n}')
    head = ('SYSTEM_PROVENANCE_CHANNELS = frozenset({\n    "operation-trigger",')
    assert tail in src and head in src, \
        "anchor not found (make_a_gated_channel_unsatisfiable)"
    return src.replace(
        tail,
        '    "trek-leader-digest": ProvenanceRequirement(\n'
        '        has_server_minted_envelope, _TREK_ADVICE),\n'
        '    "nobody-can-satisfy-me": ProvenanceRequirement(lambda ev: False, "x"),\n}',
        1).replace(
        head,
        'SYSTEM_PROVENANCE_CHANNELS = frozenset({\n'
        '    "nobody-can-satisfy-me",\n    "operation-trigger",', 1)


def point_a_channel_at_the_wrong_predicate(src: str) -> str:
    """trek-trigger の述語を operation 用に差し替える (分岐の形は残る)。

    **独立 AX レビュー (high) が実証した変異**。本物のサーバ署名イベントが永久に
    降格されるのに、kept 経路を測るテストが trek-trigger に無かったため全件緑だった
    (照合したら 5 channel 中 3 つにしか kept テストが無かった)。
    """
    anchor = ('    "trek-trigger": ProvenanceRequirement(\n'
              '        has_server_minted_envelope, _TREK_ADVICE),')
    assert anchor in src, "anchor not found (point_a_channel_at_the_wrong_predicate)"
    return src.replace(
        anchor,
        '    "trek-trigger": ProvenanceRequirement(\n'
        '        has_approved_operation_envelope, _TREK_ADVICE),', 1)


# --- scripts/codex-inbox-hook.py への追加の壊し方 ---------------------------

def codex_freezes_a_byte_identical_copy(src: str) -> str:
    """Codex 側に **今日の文言と byte 一致する** 凍結コピーを埋め込む。

    等値比較 (``codex の出力 == bd.downgrade_advice(...)``) では **捕まらない** 変異。
    私は最初その等値比較を書いて偽の安全を作り、この変異で緑を踏んだ。真値源を試験中
    だけ差し替えて出力が追随するかを見る形に直したので、いまは赤くなる。
    """
    anchor = '        f"{bd.downgrade_advice(bd.DOWNGRADE_NON_SYSTEM_ENVELOPE, ch)}"'
    assert anchor in src, "anchor not found (codex_freezes_a_byte_identical_copy)"
    return src.replace(
        anchor,
        '        f"{_FROZEN_ADVICE.get(ch) or bd.downgrade_advice(bd.DOWNGRADE_NON_SYSTEM_ENVELOPE, ch)}"',
        1).replace(
        'def render_provenance_advice_lines(channels, *, bd) -> str:',
        '_FROZEN_ADVICE = {"operation-trigger": "frozen copy"}\n\n\n'
        'def render_provenance_advice_lines(channels, *, bd) -> str:', 1)


def codex_folds_all_channels_into_one_line(src: str) -> str:
    """Codex 側が channel ごとの案内をやめ、1 つの固定文に畳む形。

    理由ごとの件数で束ねる通知なので、以前はここで必ず片方が誤案内になっていた。
    混在バッチのテストが赤くなるはず。
    """
    anchor = '    if bd is None or not channels:'
    assert anchor in src, "anchor not found (codex_folds_all_channels_into_one_line)"
    return src.replace(anchor, '    if True:', 1)


# --- ms-185 e-7078: ゲートの張り替えを守る反転 -------------------------------
#
# 張り替えで新しく主張した性質は 4 つ。どれも「壊したら赤くなる」ことを測る。
# 偽の緑を 1 度生んだ箇所なので、ここを無ガードで置くのは特に危ない。

def accept_the_retired_proof_again(src: str) -> str:
    """operation-trigger が T1-system 署名を再び受け付けるようにする。

    **これが張り替え前の状態そのもの** = 本番が一度も満たせない証拠を要求し、
    試験だけが合成した形で緑になる偽の緑。
    """
    anchor = ("    OPERATION_TRIGGER_CHANNEL: ProvenanceRequirement(\n"
              "        has_approved_operation_envelope, _OPERATION_ADVICE),")
    assert anchor in src, "anchor not found (accept_the_retired_proof_again)"
    return src.replace(
        anchor,
        "    OPERATION_TRIGGER_CHANNEL: ProvenanceRequirement(\n"
        "        has_server_minted_envelope, _OPERATION_ADVICE),", 1)


def drop_the_scope_match(src: str) -> str:
    """承認が「いま発火する op_id」を名指しているかを見なくする。

    op-1 への承認で op-9 の自律実行が通る状態。承認自体は本物なので、
    封筒の有無を見るだけの検査では捕まえられない。
    """
    anchor = ("    return bool(scope) and bool(op_id) and scope == op_id")
    assert anchor in src, "anchor not found (drop_the_scope_match)"
    return src.replace(anchor, "    return True", 1)


def treat_absence_as_a_match(src: str) -> str:
    """scope も op_id も無いときに一致と見なす (= 不在を合致に畳む)。

    ms-184 が期間フィルタで踏んだのと同じ型。空 == 空 で通ると、scope の無い
    封筒が op_id の無いトリガーを承認してしまう。
    """
    anchor = ("    return bool(scope) and bool(op_id) and scope == op_id")
    assert anchor in src, "anchor not found (treat_absence_as_a_match)"
    return src.replace(anchor, "    return scope == op_id", 1)


def fail_open_on_undeclared_channel(src: str) -> str:
    """要求を宣言していない channel に何も求めない (= 開いた門を閉じて見せる)。

    **実装中に私が実際に書いた形。** 反転ハーネスがこれを捕まえ、順序の反転と
    opt-in の反転という既存 2 件の歯が同時に抜けていたことが分かった。
    """
    anchor = "    if requirement is None:\n        return False"
    assert anchor in src, "anchor not found (fail_open_on_undeclared_channel)"
    return src.replace(
        anchor, "    if requirement is None:\n        return True", 1)


def let_the_set_and_map_drift(src: str) -> str:
    """gated 集合に、要求を宣言していない channel を足す。

    集合と要求表がズレた状態。fail-closed なので実害は「黙って降格し続ける」で、
    まさに e-7078 が消した「誰も満たせない判定」の再生産。
    """
    anchor = 'SYSTEM_PROVENANCE_CHANNELS = frozenset({\n    "operation-trigger",'
    assert anchor in src, "anchor not found (let_the_set_and_map_drift)"
    return src.replace(
        anchor,
        'SYSTEM_PROVENANCE_CHANNELS = frozenset({\n    "nobody-declares-me",'
        '\n    "operation-trigger",', 1)


CASES = [
    # (ラベル, 対象ファイル, 壊し方, 赤くなるべきテスト)
    # --- ms-185 e-7078: 張り替えたゲートを守る ---
    ("operation-trigger が引退した証拠を再び受け付ける (= 偽の緑に戻す)", BD,
     accept_the_retired_proof_again,
     PINNED_TEST + "::TestClassifyAutoExecute::"
     "test_retired_server_minted_shape_no_longer_passes"),
    ("承認が発火する op_id を名指しているかを見ない", BD, drop_the_scope_match,
     PINNED_TEST + "::TestClassifyAutoExecute::"
     "test_approval_for_a_different_operation_is_rejected"),
    ("不在を合致に畳む (scope も op_id も空で通す)", BD, treat_absence_as_a_match,
     PINNED_TEST + "::TestHasApprovedOperationEnvelope::"
     "test_empty_scope_and_empty_op_id_do_not_match"),
    ("要求未宣言の channel に何も求めない (= 開いた門を閉じて見せる)", BD,
     fail_open_on_undeclared_channel,
     PINNED_TEST + "::TestHasRequiredProvenance::"
     "test_undeclared_channel_fails_closed"),
    ("gated 集合と要求表をズラす", BD, let_the_set_and_map_drift,
     PINNED_TEST + "::TestHasRequiredProvenance::"
     "test_gated_channels_match_their_requirements"),
    ("どちらの集合からも落とす (= 本番の事故そのもの)", BD, drop_from_both_sets,
     GUARD_TEST + "::TestClassificationCoverage::"
     "test_every_known_gated_channel_is_classified"),
    ("現役と引退済みの両方に入れる", BD, retire_without_removing,
     GUARD_TEST + "::TestClassificationCoverage::test_the_two_sets_do_not_overlap"),
    ("引退の判定そのものを外す", BD, remove_the_retired_check,
     GUARD_TEST + "::TestRetiredChannelIsDowngraded::"
     "test_retired_channel_downgraded_even_when_allowlisted"),
    ("引退の判定を provenance の後ろに置く", BD, move_retired_check_after_provenance,
     GUARD_TEST + "::TestRetiredChannelIsDowngraded::"
     "test_retired_wins_over_provenance_when_a_caller_passes_both"),
    ("理由タグを既存のものと衝突させる", BD, collide_the_reason_tag,
     GUARD_TEST + "::TestReasonTagIsConsumable::"
     "test_reason_tag_is_exported_and_distinct"),
    ("独自 channel の opt-in を署名必須にする (silent scope 縮小)", BD,
     break_custom_channel_optin,
     PINNED_TEST + "::TestClassifyAutoExecute::"
     "test_non_provenance_channel_kept_without_envelope"),
    ("消費側が引退を allowlist-miss に畳む", CODEX,
     codex_folds_retired_into_allowlist_miss,
     CONSUMER_TEST + "::TestCodexTallyIsWiredToTheRightCounter"),
    ("配線だけを逆転させる (トークンは一切変えない)", CODEX,
     codex_swaps_the_counter_wiring,
     CONSUMER_TEST + "::TestCodexTallyIsWiredToTheRightCounter"),
    ("未知の理由を allowlist-miss に畳む", CODEX,
     codex_folds_unknown_into_allowlist_miss,
     CONSUMER_TEST + "::TestCodexTallyIsWiredToTheRightCounter::"
     "test_unknown_reason_is_not_folded_into_allowlist_miss"),
    ("Claude 側を固定文言に戻す (AX の misleading)", CLAUDE,
     claude_reverts_to_the_fixed_message,
     CONSUMER_TEST + "::TestClaudeHookRendersPerReasonAdvice"),
    # --- ms-185 / #812 レビュー: 案内が channel ごとに導かれることを守る ---
    # 壊すのは retired の案内なので、赤くなるべきは 4 理由を描画で見るクラス
    # (TestProvenanceAdviceIsChannelAware は証拠欠落の理由だけを見ているので、
    #  ここを指すと「壊したのに緑」になる = 最初それで MISS を踏んだ)。
    ("案内文だけを逆転させる (判定条件は触らない)", BD,
     swap_the_retired_and_provenance_advice,
     CONSUMER_TEST + "::TestClaudeHookRendersPerReasonAdvice"),
    ("証拠欠落の案内が channel を見ない (= high の状態そのもの)", BD,
     advice_ignores_the_channel,
     CONSUMER_TEST + "::TestProvenanceAdviceIsChannelAware::"
     "test_operation_trigger_names_the_approval_recovery"),
    ("ゲートした channel が回復手段を宣言しない", BD,
     drop_the_advice_from_the_requirement,
     CONSUMER_TEST + "::TestProvenanceAdviceIsChannelAware::"
     "test_every_gated_channel_declares_a_recovery"),
    ("Claude 側が共有層を呼ばず自分の固定文を持つ", CLAUDE,
     claude_keeps_its_own_copy_of_the_advice,
     CONSUMER_TEST + "::TestBothHooksShareOneAdviceSource"),
    ("Codex 側が channel ごとの案内を 1 つに畳む", CODEX,
     codex_folds_all_channels_into_one_line,
     "tests/test_codex_inbox_autonomy_e5803.py::"
     "test_mixed_provenance_batch_names_each_channels_own_recovery"),
    # --- #812 独立レビュー (AX high / medium) で塞いだ性質を守る反転 ---
    ("満たせない門を足す (宣言だけ測っていた偽の緑)", BD,
     make_a_gated_channel_unsatisfiable,
     GUARD_TEST + "::TestGatedChannelsAreActuallySatisfiable"),
    ("channel の述語を別物に差し替える (本物の封筒が拒否される)", BD,
     point_a_channel_at_the_wrong_predicate,
     GUARD_TEST + "::TestGatedChannelsAreActuallySatisfiable::"
     "test_a_properly_proven_event_is_kept_with_default_args"),
    ("Codex に byte 一致の凍結コピーを埋める (等値比較では捕まらない)", CODEX,
     codex_freezes_a_byte_identical_copy,
     CONSUMER_TEST + "::TestBothHooksShareOneAdviceSource::"
     "test_codex_hook_follows_the_shared_source"),
]


BASELINE = [GUARD_TEST, PINNED_TEST, CONSUMER_TEST]


# 赤いこと自体はガードが効いた証拠にならない (変異が構文を壊しても赤くなる)。
# 赤の理由の判定は ``invert_harness.classify_pytest_result`` が 1 箇所で持つ。
def run(target: str) -> tuple:
    return ih.run_and_classify(target, ROOT)


def main() -> int:
    originals = {p: p.read_text(encoding="utf-8") for p in (BD, CODEX, CLAUDE)}
    if not ih.check_baseline(BASELINE, ROOT):
        return 1

    bad = 0
    for label, path, mutate, target in CASES:
        try:
            path.write_text(mutate(originals[path]), encoding="utf-8")
            _invalidate_bytecode_cache(path)
            went_red, tail = run(target)
        except AssertionError as exc:
            _restore(originals)
            print("  SKIP  %s: 当てる場所が無い (%s) — SKIP は緑と同じ害なので、"
                  "壊し方を実装に合わせて直すこと" % (label, exc))
            bad += 1
            continue
        finally:
            _restore(originals)
        print(("  OK  " if went_red else " MISS ") + label)
        print("          → %s" % tail)
        if not went_red:
            bad += 1

    print()
    if bad:
        print("NG: %d 件が「壊したのに赤くならない」か SKIP。偽の安全なので直すこと。" % bad)
        return 1
    # **ハーネスが環境を汚したまま終わらないことを、最後に実測して確かめる。**
    # 変異と復元を繰り返すので、復元漏れ / バイトコードキャッシュの残留が起きうる。
    # 黙って汚して終わると、次に走る人が「ソースは正しいのにテストが落ちる」
    # (または通る) 状態を引き継ぐ = ハーネス自身が偽の安全を作る。
    if not ih.check_not_polluted(BASELINE, ROOT):
        return 1
    print()
    print("OK: %d 件すべて、守る対象を壊すと赤くなった。" % len(CASES))
    return 0


def _final_cleanup(originals: dict) -> None:
    """ハーネスが環境を汚したまま終わらないことを保証する。"""
    _restore(originals)


if __name__ == "__main__":
    sys.exit(ih.guarded_main(main, sys.argv, __doc__, ROOT))
