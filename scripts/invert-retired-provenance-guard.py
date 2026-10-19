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
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
BD = ROOT / "lib" / "bus_delivery.py"
CODEX = ROOT / "scripts" / "codex-inbox-hook.py"
GUARD_TEST = "tests/test_retired_provenance_channels_e7077.py"
PINNED_TEST = "tests/test_bus_delivery.py"


# --- lib/bus_delivery.py への壊し方 -----------------------------------------

def drop_from_provenance_only(src: str) -> str:
    """trek-trigger を provenance から消すだけ (= 引退先に足し忘れる)。

    **これが本番の事故そのもの。** 畳む作業で起きる最も自然な間違いで、古い opt-in が
    残っているプロジェクトで署名なし自動実行が通る。
    """
    anchor = '    "trek-trigger",\n    "trek-progress-check",'
    assert anchor in src, "anchor not found (drop_from_provenance_only)"
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
            "has_system_provenance(event):\n"
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
              "has_system_provenance(event):")
    assert anchor in src, "anchor not found (break_custom_channel_optin)"
    return src.replace(anchor, "    if not has_system_provenance(event):", 1)


# --- scripts/codex-inbox-hook.py への壊し方 ---------------------------------

def codex_folds_retired_into_allowlist_miss(src: str) -> str:
    """消費側が引退の理由を allowlist-miss に畳む (= 「opt-in しろ」と誤案内する)。

    足す前の 2-way 分岐に戻した状態。理由を足しても消費側が追従していなければ
    「足した」≠「全域に効く」になる。
    """
    anchor = ('                elif reason == getattr(bd, '
              '"DOWNGRADE_RETIRED_CHANNEL", None):\n'
              '                    downgraded_retired += 1\n')
    assert anchor in src, "anchor not found (codex_folds_retired_into_allowlist_miss)"
    return src.replace(anchor, "", 1)


CASES = [
    # (ラベル, 対象ファイル, 壊し方, 赤くなるべきテスト)
    ("provenance から消すだけ (= 本番の事故そのもの)", BD, drop_from_provenance_only,
     GUARD_TEST + "::TestClassificationCoverage::"
     "test_every_known_server_minted_channel_is_classified"),
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
     "tests/test_codex_retired_channel_notice_e7077.py"),
]


def run(target: str) -> tuple:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", target, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True)
    tail = (r.stdout + r.stderr).strip().split("\n")[-1]
    return (r.returncode != 0, tail)


def main() -> int:
    originals = {p: p.read_text(encoding="utf-8") for p in (BD, CODEX)}

    base = subprocess.run(
        [sys.executable, "-m", "pytest", GUARD_TEST, PINNED_TEST,
         "tests/test_codex_retired_channel_notice_e7077.py",
         "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True)
    if base.returncode != 0:
        print("BASELINE RED — 壊す前からガードが落ちている。先にそれを直すこと。")
        print((base.stdout + base.stderr)[-2000:])
        return 1
    print("baseline: " + base.stdout.strip().splitlines()[-1] + "\n")

    bad = 0
    for label, path, mutate, target in CASES:
        try:
            path.write_text(mutate(originals[path]), encoding="utf-8")
            went_red, tail = run(target)
        except AssertionError as exc:
            print("  SKIP  %s: 当てる場所が無い (%s) — SKIP は緑と同じ害なので、"
                  "壊し方を実装に合わせて直すこと" % (label, exc))
            bad += 1
            continue
        finally:
            for p, text in originals.items():
                p.write_text(text, encoding="utf-8")
        print(("  OK  " if went_red else " MISS ") + label)
        print("          → %s" % tail)
        if not went_red:
            bad += 1

    print()
    if bad:
        print("NG: %d 件が「壊したのに赤くならない」か SKIP。偽の安全なので直すこと。" % bad)
        return 1
    print("OK: %d 件すべて、守る対象を壊すと赤くなった。" % len(CASES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
