#!/usr/bin/env python3
"""事前承認済み channel のガードが「守る対象を壊したとき実際に赤くなるか」を
1 件ずつ測る反転ハーネス (ms-185 e-7067)。

緑のガードは信頼されるので、**偽の安全は無ガードより悪い**。このスクリプトは
``lib/dm_qualgate.py`` / ``channel/bus-qualgate.mjs`` / ``lib/dm_consent.py`` に
1 箇所ずつ壊し方を当てて pytest を走らせ、意図したテストが赤くなることを確認する。

壊し方は 2 方向を網羅する:
  * **緩める** (前方一致を戻す / 一覧に trek-* を足す / 片方の実装だけ戻す)
    → 確認を省く根拠の無い経路が復活する = 危険側
  * **締めすぎる** (一覧ごと消す / operation-trigger を外す)
    → Operation の事前承認が落ちる = 逆側に壊れる

片方だけ見ていると、もう片方に倒したときに気付けない。

バイトコードキャッシュの隔離と、実行後にベースラインが緑へ戻ることの実測は
``scripts/invert_harness.py`` が持つ (なぜその形かの経緯もそこに在る)。

使い方: python3 scripts/invert-pre-approved-channel-guard.py
終了コード: 全ケースで期待どおり赤くなれば 0、1 件でも「壊したのに緑」なら 1。
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import invert_harness as ih  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
PY_GATE = ROOT / "lib" / "dm_qualgate.py"
JS_GATE = ROOT / "channel" / "bus-qualgate.mjs"
CONSENT = ROOT / "lib" / "dm_consent.py"

GUARD_TEST = "tests/test_pre_approved_channel_exact_match_e7067.py"
CONSENT_TEST = "tests/test_dm_consent.py"
PARITY_TEST = "tests/test_dm_qualgate_parity.py"
JS_TEST = "tests/test_bus_qualgate.py"
ALIGN_TEST = "tests/test_dm_consent_skill_alignment_e6349.py"
BASELINE = [GUARD_TEST, CONSENT_TEST, PARITY_TEST, JS_TEST, ALIGN_TEST]


# --- 緩める方向 (危険側) ----------------------------------------------------

def py_restore_prefix_match(src: str) -> str:
    """Python 側に ``startswith("trek-")`` の前方一致を戻す (= 元の穴そのもの)。"""
    anchor = '    return str(channel or "") in PRE_APPROVED_CHANNELS'
    assert anchor in src, "anchor not found (py_restore_prefix_match)"
    return src.replace(
        anchor,
        '    c = str(channel or "")\n'
        '    return c.startswith("trek-") or c in PRE_APPROVED_CHANNELS  # MUTANT',
        1)


def py_add_trek_to_the_list(src: str) -> str:
    """前方一致ではなく、一覧に trek-* を足し直す (= 名指しで緩める)。"""
    anchor = 'PRE_APPROVED_CHANNELS = frozenset({\n    "operation-trigger",\n})'
    assert anchor in src, "anchor not found (py_add_trek_to_the_list)"
    return src.replace(
        anchor,
        'PRE_APPROVED_CHANNELS = frozenset({\n    "operation-trigger",\n'
        '    "trek-trigger",  # MUTANT\n})', 1)


def py_prefix_match_on_the_allowed_name(src: str) -> str:
    """完全一致を前方一致に変える (= operation-trigger-evil が通る)。"""
    anchor = '    return str(channel or "") in PRE_APPROVED_CHANNELS'
    assert anchor in src, "anchor not found (py_prefix_match_on_the_allowed_name)"
    return src.replace(
        anchor,
        '    c = str(channel or "")\n'
        '    return any(c.startswith(p) for p in PRE_APPROVED_CHANNELS)  # MUTANT',
        1)


def js_restore_prefix_match(src: str) -> str:
    """JS 側だけ前方一致を戻す (= 2 実装の片側だけ緩い状態 = Codex と Claude で差)。"""
    anchor = "  return PRE_APPROVED_CHANNELS.has(String(channel || ''))"
    assert anchor in src, "anchor not found (js_restore_prefix_match)"
    return src.replace(
        anchor,
        "  const c = String(channel || '')\n"
        "  return c.startsWith('trek-') || PRE_APPROVED_CHANNELS.has(c)  // MUTANT",
        1)


def js_add_trek_to_the_list(src: str) -> str:
    """JS 側の一覧にだけ trek-* を足す (= 一覧の drift)。"""
    anchor = "export const PRE_APPROVED_CHANNELS = new Set([\n  'operation-trigger',\n])"
    assert anchor in src, "anchor not found (js_add_trek_to_the_list)"
    return src.replace(
        anchor,
        "export const PRE_APPROVED_CHANNELS = new Set([\n  'operation-trigger',\n"
        "  'trek-trigger', // MUTANT\n])", 1)


# --- 締めすぎる方向 (逆側に壊す) -------------------------------------------

def py_empty_the_list(src: str) -> str:
    """一覧ごと空にする (= Operation の事前承認が落ちる)。"""
    anchor = 'PRE_APPROVED_CHANNELS = frozenset({\n    "operation-trigger",\n})'
    assert anchor in src, "anchor not found (py_empty_the_list)"
    return src.replace(anchor, "PRE_APPROVED_CHANNELS = frozenset()  # MUTANT", 1)


def js_empty_the_list(src: str) -> str:
    """JS 側の一覧を空にする。"""
    anchor = "export const PRE_APPROVED_CHANNELS = new Set([\n  'operation-trigger',\n])"
    assert anchor in src, "anchor not found (js_empty_the_list)"
    return src.replace(
        anchor, "export const PRE_APPROVED_CHANNELS = new Set([]) // MUTANT", 1)


# --- 判定の再利用をやめる (一覧が 2 箇所に増える) --------------------------

def consent_keeps_its_own_list(src: str) -> str:
    """dm_consent が dm_qualgate を再利用せず自前の判定を持つ。

    一覧が 2 箇所になると片方だけ直されるので、qualgate を締めても送信側の
    免除だけ緩いまま残る形が作れる。"""
    anchor = "def _is_pre_approved_channel(channel: str) -> bool:\n    return _qualgate.is_pre_approved_channel(channel)"
    assert anchor in src, "anchor not found (consent_keeps_its_own_list)"
    return src.replace(
        anchor,
        "def _is_pre_approved_channel(channel: str) -> bool:  # MUTANT\n"
        '    c = str(channel or "")\n'
        '    return c.startswith("trek-") or c == "operation-trigger"',
        1)


def consent_drops_the_retired_vocabulary(src: str) -> str:
    """引退した判定理由の説明を消す (= 過去の監査記録が読めなくなる)。"""
    anchor = '    "trek_scoped_channel":'
    assert anchor in src, "anchor not found (consent_drops_the_retired_vocabulary)"
    i = src.index("RETIRED_CONSENT_REASON_EXPLANATIONS = {")
    j = src.index("}", src.index(anchor)) + 1
    return src[:i] + "RETIRED_CONSENT_REASON_EXPLANATIONS = {}  # MUTANT" + src[j:]


CASES = [
    # 緩める方向
    ("Python に前方一致を戻す (元の穴)", PY_GATE, py_restore_prefix_match,
     GUARD_TEST + "::test_prefixed_name_is_not_pre_approved_python"),
    ("Python に前方一致を戻す (armed 保留の経路)", PY_GATE, py_restore_prefix_match,
     GUARD_TEST + "::test_prefixed_name_is_held_by_the_armed_qual_gate"),
    ("Python の一覧に trek-* を足す", PY_GATE, py_add_trek_to_the_list,
     GUARD_TEST + "::test_the_list_is_exactly_operation_trigger"),
    ("完全一致を許可名の前方一致にする", PY_GATE, py_prefix_match_on_the_allowed_name,
     GUARD_TEST + "::test_prefixed_name_is_not_pre_approved_python"),
    ("JS だけ前方一致を戻す (2 実装の非対称)", JS_GATE, js_restore_prefix_match,
     GUARD_TEST + "::test_prefixed_name_is_not_pre_approved_js"),
    ("JS だけ前方一致を戻す (振る舞いの一致)", JS_GATE, js_restore_prefix_match,
     PARITY_TEST),
    ("JS の一覧にだけ trek-* を足す", JS_GATE, js_add_trek_to_the_list,
     GUARD_TEST + "::test_the_two_implementations_hold_the_same_list"),
    # 締めすぎる方向
    ("Python の一覧ごと空にする (Operation が落ちる)", PY_GATE, py_empty_the_list,
     GUARD_TEST + "::test_operation_trigger_still_skips_consent_on_the_send_path"),
    ("JS の一覧ごと空にする", JS_GATE, js_empty_the_list,
     GUARD_TEST + "::test_operation_trigger_still_pre_approved_js"),
    # 一覧を 2 箇所に増やす / 記録を読めなくする
    ("送信側が自前の判定を持つ", CONSENT, consent_keeps_its_own_list,
     GUARD_TEST + "::test_prefixed_name_is_not_exempted_as_pre_approved_on_the_send_path"),
    ("引退した判定理由の説明を消す", CONSENT, consent_drops_the_retired_vocabulary,
     GUARD_TEST + "::test_retired_reason_string_is_still_explainable"),
]

# 赤いこと自体はガードが効いた証拠にならない (変異が構文を壊しても赤くなる)。
# 赤の理由の判定は ``invert_harness.classify_pytest_result`` が 1 箇所で持つ。
def run_one(target: str):
    return ih.run_and_classify(target, ROOT)


def main() -> int:
    originals = {p: p.read_text(encoding="utf-8") for p in (PY_GATE, JS_GATE, CONSENT)}
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
        print(f"NG: {bad} 件が「壊したのに赤くならない」か SKIP。偽の安全なので直すこと。")
        return 1
    if not ih.check_not_polluted(BASELINE, ROOT):
        return 1
    print()
    print(f"OK: {len(CASES)} 件すべて、守る対象を壊すと赤くなった。")
    return 0


if __name__ == "__main__":
    sys.exit(ih.guarded_main(main, sys.argv, __doc__, ROOT))
