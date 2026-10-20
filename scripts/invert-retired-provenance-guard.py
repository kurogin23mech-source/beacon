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
def _isolated_env() -> dict:
    return ih.isolated_env()


def _invalidate_bytecode_cache(path: pathlib.Path) -> None:
    ih.invalidate_bytecode_cache(path, ROOT)


def _restore(originals: dict) -> None:
    ih.restore(originals, ROOT)


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
    old = ('            f" — {_downgrade_advice(ev.get(\'_downgrade_reason\', \'\'))}]"')
    new = ('            " — channel not in bus_auto_execute_channels]"')
    assert old in src, "anchor not found (claude_reverts_to_the_fixed_message)"
    return src.replace(old, new, 1)


def claude_swaps_retired_and_provenance_advice(src: str) -> str:
    """Claude 側の案内文だけを逆転させる (条件は触らない)。

    「署名が無い」に対して「古い opt-in を外せ」、引退に対して「直せません」と
    案内する状態。分岐の形は残るので、分岐の存在を見るだけでは捕まえられない。
    """
    prov = ('        return ("channel は allowlist 済だが envelope が T1-system でない "\n'
            '                "(= 偽装の恐れ)。opt-in し直しても直りません")')
    ret = ('        return ("channel は allowlist 済だが、サーバが発行する channel では "\n'
           '                "なくなっています (= 古い opt-in が残った状態)。"\n'
           '                "`beacon bus auto-execute remove --channel <名前>` で外してください")')
    assert prov in src and ret in src, \
        "anchor not found (claude_swaps_retired_and_provenance_advice)"
    return src.replace(prov, '@@P@@', 1).replace(ret, prov, 1).replace('@@P@@', ret, 1)


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
    ("Claude 側の案内文だけを逆転させる", CLAUDE,
     claude_swaps_retired_and_provenance_advice,
     CONSUMER_TEST + "::TestClaudeHookRendersPerReasonAdvice"),
]


def run(target: str) -> tuple:
    r = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", target, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, env=_isolated_env())
    tail = (r.stdout + r.stderr).strip().split("\n")[-1]
    return (r.returncode != 0, tail)


def main() -> int:
    originals = {p: p.read_text(encoding="utf-8") for p in (BD, CODEX, CLAUDE)}

    base = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", GUARD_TEST, PINNED_TEST,
         CONSUMER_TEST,
         "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, env=_isolated_env())
    if base.returncode != 0:
        print("BASELINE RED — 壊す前からガードが落ちている。先にそれを直すこと。")
        print((base.stdout + base.stderr)[-2000:])
        return 1
    print("baseline: " + base.stdout.strip().splitlines()[-1] + "\n")

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
    after = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", GUARD_TEST, PINNED_TEST,
         CONSUMER_TEST, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, env=_isolated_env())
    if after.returncode != 0:
        print()
        print("NG: ハーネス実行後にベースラインが緑に戻りません。復元漏れか、")
        print("    バイトコードキャッシュの残留です。環境を汚したまま終わるのは")
        print("    偽の安全そのものなので失敗として扱います。")
        print((after.stdout + after.stderr)[-1500:])
        return 1
    print("after: " + after.stdout.strip().splitlines()[-1]
          + "  (= 環境を汚していない)")
    print()
    print("OK: %d 件すべて、守る対象を壊すと赤くなった。" % len(CASES))
    return 0


def _final_cleanup(originals: dict) -> None:
    """ハーネスが環境を汚したまま終わらないことを保証する。"""
    _restore(originals)


if __name__ == "__main__":
    sys.exit(main())
