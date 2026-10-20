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

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
BD = ROOT / "lib" / "bus_delivery.py"
CODEX = ROOT / "scripts" / "codex-inbox-hook.py"
CLAUDE = ROOT / "bin" / "beacon-bus-inbox-hook.py"
GUARD_TEST = "tests/test_retired_provenance_channels_e7077.py"
PINNED_TEST = "tests/test_bus_delivery.py"
CONSUMER_TEST = "tests/test_codex_retired_channel_notice_e7077.py"

# この実行専用のバイトコードキャッシュ置き場。
#
# **なぜ要るか (2026-10-19 実測)**: macOS 同梱の Python は
# ``~/Library/Caches/com.apple.python/<ソースの絶対パス>`` に ``.pyc`` を書く。
# このハーネスは同じパスのファイルを変異させては戻すので、**変異版の ``.pyc`` が
# 共有キャッシュに残り、ハーネス実行後の通常のテストがソースと食い違う結果を返す**。
# 実測ではハーネス後に単体テストが 2 件、偽の赤になった。向きが逆なら**偽の緑**にも
# なる = ハーネス自身が「偽の安全」を作る。キャッシュを後から消す形は、subprocess が
# 書く時刻とこちらが消す時刻が競合して取りこぼす。**書かせる場所を毎回変える**方が確実。
_RUN_CACHE_DIR = pathlib.Path(tempfile.mkdtemp(prefix="invert-pycache-"))


def _isolated_env() -> dict:
    """バイトコードキャッシュを隔離した環境変数 (上の ``_RUN_CACHE_DIR`` 参照)。"""
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPYCACHEPREFIX"] = str(_RUN_CACHE_DIR)
    return env


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


# --- ms-185 e-7078: ゲートの張り替えを守る反転 -------------------------------
#
# 張り替えで新しく主張した性質は 4 つ。どれも「壊したら赤くなる」ことを測る。
# 偽の緑を 1 度生んだ箇所なので、ここを無ガードで置くのは特に危ない。

def accept_the_retired_proof_again(src: str) -> str:
    """operation-trigger が T1-system 署名を再び受け付けるようにする。

    **これが張り替え前の状態そのもの** = 本番が一度も満たせない証拠を要求し、
    試験だけが合成した形で緑になる偽の緑。
    """
    anchor = "    OPERATION_TRIGGER_CHANNEL: has_approved_operation_envelope,"
    assert anchor in src, "anchor not found (accept_the_retired_proof_again)"
    return src.replace(
        anchor, "    OPERATION_TRIGGER_CHANNEL: has_server_minted_envelope,", 1)


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
    ("Claude 側の案内文だけを逆転させる", CLAUDE,
     claude_swaps_retired_and_provenance_advice,
     CONSUMER_TEST + "::TestClaudeHookRendersPerReasonAdvice"),
]


def _invalidate_bytecode_cache(path: pathlib.Path) -> None:
    """``path`` のバイトコードキャッシュを無効化する。

    **なぜ必要か (2026-10-19 に実測)**: このハーネスは対象ファイルを書き換えて
    pytest を走らせる。Apple の Python は ``~/Library/Caches/com.apple.python/``
    配下に、ソースの**絶対パスをそのまま掘った**場所へ ``.pyc`` を書く。復元して
    内容を元に戻しても、**変異版の ``.pyc`` が再利用されることがある** — 実測では
    ハーネス実行後に単体テストが 2 件落ちた (ソースは正しいのに、入れ替えた
    案内文が返ってきた)。これは単なるフレークより悪い: **偽の赤**を作り、
    変異の向きによっては**偽の緑**も作れる。``python3 -B`` は書き込みを止めるだけで
    読み込みは止めないので対策にならない。

    2 重に無効化する: (1) キャッシュの実体を ``.trash/`` へ退避する
    (このプロジェクトは ``rm`` 禁止なので消さずに移す)、(2) ソースの更新時刻を
    先に進めてキャッシュ鍵を確実に変える。
    """
    # (1) Apple のキャッシュ実体を退避
    cache_root = pathlib.Path.home() / "Library" / "Caches" / "com.apple.python"
    if cache_root.is_dir():
        # キャッシュは絶対パスをそのまま掘った場所に置かれる
        mirrored = cache_root / str(path.resolve()).lstrip("/")
        for cached in mirrored.parent.glob(mirrored.stem + ".*.pyc"):
            trash = ROOT / ".trash" / "pycache-invert-harness"
            trash.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(cached), str(trash / cached.name))
            except OSError:
                pass
    # (2) 更新時刻を先に進めてキャッシュ鍵を変える
    try:
        future = time.time() + 2
        os.utime(path, (future, future))
    except OSError:
        pass


def _restore(originals: dict) -> None:
    """全対象ファイルを元に戻し、バイトコードキャッシュも無効化する。"""
    for p, text in originals.items():
        p.write_text(text, encoding="utf-8")
        _invalidate_bytecode_cache(p)


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
