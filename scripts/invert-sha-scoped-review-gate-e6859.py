#!/usr/bin/env python3
"""レビュー済みの帳簿を sha で持つ機構が「壊したとき実際に赤くなるか」を測る
反転ハーネス (ms-119 e-6859)。

**この機構は『緑なのに未レビュー』を消すために在る。** だから守る試験が偽の緑を
出していたら、直す前より悪い (緑のガードは信頼されるので)。e-6859 の受入条件は
「実施後にコミットを 1 本積むとゲートが赤に戻ることを **実測で** 確認すること
(test-the-test)」を明示的に求めているので、ここがその実測。

**初回の実測で、自分で書いた試験のうち 2 件が偽の緑だった** (2026-11-05):

* ``sha_pinned is False`` の枝を丸ごと消しても緑 — 試験が sha 空の記録しか
  見ておらず、「sha 空」の別の枝が代わりに拾っていた。枝が本当に効くのは
  **sha を持ちながら pin していないと名乗る記録** のときなので、その形を足した。
* 印を押す先を ``reviewed_sha`` から手元 HEAD に書き換えても緑 — 試験が 2 つを
  同じ値にしていて区別できていなかった。``git rev-parse`` が空を返す形
  (commit が 1 つも無いリポジトリ等、非 0 でも例外は飛ばないのでここに来る) で
  区別が出るので、その形を足した。

使い方: python3 scripts/invert-sha-scoped-review-gate-e6859.py
終了コード: 全ケースが期待どおりなら 0、1 件でも外れるか SKIP なら 1
(SKIP は緑と同じ害なので失敗として数える)。対象ファイルは毎ケース必ず元に戻す。

**pytest から呼ばない。** 共有ファイルを in-place で書き換えるので、CI の並列
worker が変異中のファイルを読む (ハーネス共通の穴 4、``invert_harness`` の
docstring 参照)。
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import invert_harness as ih  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent

SPINE = ROOT / "lib" / "review_spine.py"
SHARED = ROOT / "lib" / "commands_shared.py"
PR = ROOT / "lib" / "cmd_pr.py"
CMDS = ROOT / "lib" / "commands.py"

GATE_T = "tests/test_review_merge_gate.py"
SHA_T = "tests/test_review_sha_scoped_gate_e6859.py"
CI_T = "tests/test_review_gate_ci.py"
STAMP_T = "tests/test_review_gate_stamp_visibility_e6873.py"

#: 壊す前に緑であることを確かめる母集団。関門 / 導出 / 印 / 開示の 4 面すべて。
BASELINE = [GATE_T, SHA_T, CI_T, STAMP_T]


# --- 壊し方 -----------------------------------------------------------------
#
# どれも「e-6859 より前の形」か「寛容を一段緩めた形」。つまり **実際に起きていた
# 欠陥か、起きやすい緩め方** を当てる (合成的な構文破壊は当てない —— それは
# 「壊し方が下手」を「守られている」と読み違える穴 3 そのもの)。

def gate_reads_the_trigger_files_again(src: str) -> str:
    """関門を導出から「トリガーファイルの有無」に戻す = e-6859 の退行そのもの。"""
    anchor = "    ob = pr_review_obligations(pr_number, pr_repo)"
    assert anchor in src, "anchor not found (gate_reads_the_trigger_files_again)"
    return src.replace(anchor, (
        "    ob = {'owed': _pending_review_types_for_pr(pr_number, pr_repo),\n"
        "          'resolved': True, 'head_sha': '', 'pr_state': 'OPEN',\n"
        "          'satisfied': [], 'legacy_no_sha': [], 'unpinned': [],\n"
        "          'stale': [], 'skipped': [], 'provenance': []}"), 1)


def credit_a_record_regardless_of_its_sha(src: str) -> str:
    """実施記録を head と突き合わせず常に差し引く (= PR 単位の帳簿に戻る)。"""
    anchor = "        if head_known and rsha == head:"
    assert anchor in src, "anchor not found (credit_a_record_regardless_of_its_sha)"
    return src.replace(anchor, "        if head_known or True:", 1)


def treat_an_unpinned_record_like_a_legacy_one(src: str) -> str:
    """pin できなかった新記録を旧形式と同じ扱いにする。

    旧形式のための寛容が **恒久的な抜け道** に変わる形 (gh が落ちていると
    sha 無しの記録が増え、以後の全差分を覆う)。素朴に緩めると素通りする型。
    """
    anchor = "        if pinned is False:"
    assert anchor in src, "anchor not found (treat_an_unpinned_record_like_a_legacy_one)"
    return src.replace(anchor, "        if False:", 1)


def let_an_unknown_head_through_the_gate(src: str) -> str:
    """head 不明でも関門を通す (= 止める側に倒す判断を外す)。"""
    anchor = '    if not ob.get("resolved"):'
    assert anchor in src, "anchor not found (let_an_unknown_head_through_the_gate)"
    return src.replace(anchor, "    if False:", 1)


def write_the_ledger_without_a_sha(src: str) -> str:
    """台帳レコードが sha を持たない (= 記録側を壊す)。

    関門側が無傷でも、記録が差分を指していなければ同じ穴に戻る。
    """
    anchor = '        "reviewed_sha": str(reviewed_sha or "").strip(),'
    assert anchor in src, "anchor not found (write_the_ledger_without_a_sha)"
    return src.replace(anchor, '        "reviewed_sha": "",', 1)


def stamp_the_local_head_instead_of_the_reviewed_sha(src: str) -> str:
    """印を「レビューした sha」でなく手元 HEAD に押す。"""
    anchor = '                            "--sha", reviewed_sha, "--pr", pr_number],'
    assert anchor in src, "anchor not found (stamp_the_local_head_instead_of_the_reviewed_sha)"
    return src.replace(anchor, '                            "--sha", local_sha, "--pr", pr_number],', 1)


def stamp_without_checking_every_required_type(src: str) -> str:
    """全種別の充足を見ずに印を押す = 1 種別の実施で今の head を緑にできた形
    (PR #785 の事故の本体)。"""
    anchor = '    if _ob.get("owed"):'
    assert anchor in src, "anchor not found (stamp_without_checking_every_required_type)"
    return src.replace(anchor, "    if False:", 1)


def read_the_skip_log_with_the_ledger_spelling(src: str) -> str:
    """免除ログを台帳の綴り (``pr_number`` / ``pr_repo``) で読む。

    書き手は ``ref`` / ``repo`` を書くので、免除したのに関門が開かない状態
    (しかも理由が表示されない) になる。
    """
    anchor = '                rec_repo = str(rec.get("repo") or "").strip()'
    assert anchor in src, "anchor not found (read_the_skip_log_with_the_ledger_spelling)"
    return src.replace(anchor, (
        '                rec_repo = str(rec.get("pr_repo") or "").strip()\n'
        '                if str(rec.get("pr_number") or "x") != pr_number:\n'
        '                    continue'), 1)


def hardcode_the_required_set(src: str) -> str:
    """必須種別を registry でなくリテラルで持つ = 関門と CI が別集合を見る形。"""
    anchor = ('    return [d["review"]\n'
              '            for d in batch_review_types_for_node("pr-open", registry=registry)]')
    assert anchor in src, "anchor not found (hardcode_the_required_set)"
    return src.replace(anchor, '    return ["ax"]', 1)


def silence_the_weakening_disclosure(src: str) -> str:
    """緑の意味を弱める開示 (旧形式 / pin 無し / 免除 / stale) を黙らせる。

    差し引いた根拠を言わなくなると、関門の緑が黙って意味を変える。
    """
    anchor = "def format_pr_review_obligation_disclosure(ob: dict) -> list:"
    assert anchor in src, "anchor not found (silence_the_weakening_disclosure)"
    return src.replace(anchor, anchor + "\n    return []", 1)


def drop_the_live_github_guard_under_test(src: str) -> str:
    """試験中の gh 遮断を外す = 試験が生きた GitHub に依存する形。

    関門がこの問いを毎回立てるので、遮断が無いと試験全体が非 hermetic になる
    (実測: 架空リポジトリ ``o/r`` で本物の GraphQL エラーが返った)。
    """
    anchor = "    if (isinstance(gh_port.get_adapter(), gh_port.SubprocessGhAdapter)"
    assert anchor in src, "anchor not found (drop_the_live_github_guard_under_test)"
    return src.replace(anchor, "    if (False", 1)


#: (表示名, 対象ファイル, 壊し方, 赤くなるべき試験)
EXPECT_RED = [
    ("関門が導出でなくトリガーファイルを読む (e-6859 の退行そのもの)",
     PR, gate_reads_the_trigger_files_again,
     f"{GATE_T}::test_gate_blocks_again_after_a_commit_is_pushed "
     f"{GATE_T}::test_gate_passes_when_all_types_reviewed_at_this_head"),

    ("実施記録の sha を head と突き合わせず常に差し引く",
     SPINE, credit_a_record_regardless_of_its_sha,
     f"{SHA_T}::test_a_review_pinned_to_another_sha_is_owed_again "
     f"{GATE_T}::test_gate_blocks_again_after_a_commit_is_pushed"),

    ("pin できなかった新記録を旧形式と同じ扱いにする (寛容が抜け道になる形)",
     SPINE, treat_an_unpinned_record_like_a_legacy_one,
     f"{SHA_T}::test_a_new_record_that_failed_to_pin_is_NOT_credited "
     f"{SHA_T}::test_an_unpinned_record_is_not_credited_EVEN_IF_its_sha_matches_head"),

    ("head 不明でも関門を通す (止める側に倒す判断を外す)",
     PR, let_an_unknown_head_through_the_gate,
     f"{GATE_T}::test_gate_blocks_when_head_cannot_be_resolved"),

    ("台帳レコードが sha を持たない (記録側を壊す)",
     SHARED, write_the_ledger_without_a_sha,
     f"{SHA_T}::test_the_ledger_record_carries_the_reviewed_sha "
     f"{GATE_T}::test_gate_blocks_again_after_a_commit_is_pushed"),

    ("印を「レビューした sha」でなく手元 HEAD に押す",
     CMDS, stamp_the_local_head_instead_of_the_reviewed_sha,
     f"{CI_T}::test_ci_flip_pushes_the_mark_to_the_reviewed_sha "
     f"{CI_T}::test_ci_flip_targets_the_reviewed_sha_not_the_local_head"),

    ("全種別の充足を見ずに印を押す (1 種別で緑にできた形)",
     CMDS, stamp_without_checking_every_required_type,
     f"{CI_T}::test_ci_flip_refuses_while_another_type_is_still_owed"),

    ("免除ログの読み口を台帳の綴りに戻す (免除が効かなくなる)",
     SHARED, read_the_skip_log_with_the_ledger_spelling,
     f"{SHA_T}::test_the_skip_reader_reads_what_the_skip_writer_writes "
     f"{GATE_T}::test_gate_credits_a_reason_bearing_skip"),

    ("必須種別を registry でなくリテラルで持つ (CI と別集合になる形)",
     SPINE, hardcode_the_required_set,
     f"{SHA_T}::test_the_required_set_comes_from_the_registry_not_a_literal"),

    ("弱める開示 (旧形式/pin無し/免除/stale) をまとめて黙らせる",
     SHARED, silence_the_weakening_disclosure,
     f"{SHA_T}::test_every_weakening_bucket_is_disclosed "
     f"{SHA_T}::test_a_satisfied_type_discloses_which_sha_and_whose_record"),

    ("試験中の gh 遮断を外す (試験が生きた GitHub に依存する形)",
     SHARED, drop_the_live_github_guard_under_test,
     f"{SHA_T}::test_resolve_pr_head_does_not_touch_live_github_under_test"),
]


def main() -> int:
    originals = {p: p.read_text(encoding="utf-8")
                 for p in (SPINE, SHARED, PR, CMDS)}
    if not ih.check_baseline(BASELINE, ROOT):
        return 1

    bad = 0
    for label, path, mutate, target in EXPECT_RED:
        try:
            path.write_text(mutate(originals[path]), encoding="utf-8")
            ih.invalidate_bytecode_cache(path, ROOT)
            went_red, tail = ih.run_and_classify(target.split(), ROOT)
        except AssertionError as exc:
            ih.restore(originals, ROOT)
            print("  SKIP  %s: 当てる場所が無い (%s) — SKIP は緑と同じ害なので、"
                  "壊し方を実装に合わせて直すこと" % (label, exc))
            bad += 1
            continue
        finally:
            ih.restore(originals, ROOT)
        print(("  OK  " if went_red else " MISS ") + label)
        print("          → %s" % tail)
        if not went_red:
            bad += 1
            print("          期待: 赤 / 実際: 緑 — 試験がこの欠陥を見ていない")

    print()
    if bad:
        print("NG: %d 件が期待と違うか SKIP。偽の安全なので直すこと。" % bad)
        return 1
    if not ih.check_not_polluted(BASELINE, ROOT):
        return 1
    print()
    print("OK: %d 件すべて壊したときに赤くなった。" % len(EXPECT_RED))
    return 0


if __name__ == "__main__":
    sys.exit(ih.guarded_main(main, sys.argv, __doc__, ROOT))
