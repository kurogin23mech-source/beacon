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
TRIG = ROOT / "lib" / "cmd_trigger.py"
OBL_STUB = ROOT / "tests" / "_pr_obligations_stub.py"
GATE_CI = ROOT / "scripts" / "review-gate-ci.py"

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
        "    ob = {'owed': _pr_open_review_nudge_pending_types(pr_number, pr_repo),\n"
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


# --- 独立レビュー (PR #838, AX + 保守性) の指摘に対する修正への壊し方 -------

def suppress_the_nudge_forever_again(src: str) -> str:
    """促しの抑止を「マーカーが在れば永久」に戻す = AX① の退行。

    judge が実機で再現した形。関門は止まるが、``BEACON_REVIEW_GATE_CI`` 未設定の
    リポジトリでは ``gh pr merge`` / UI が関門を経由しないので、促しの空リストを
    信じて直接マージ経路へ進むと、事故が読み取り専用の確認経路側から再発する。
    """
    anchor = ("    marker = reviewed_marker_sha(pr_number, pr_repo)\n"
              "    if marker is None or not marker:\n"
              "        return False")
    assert anchor in src, "anchor not found (suppress_the_nudge_forever_again)"
    return src.replace(anchor, (
        "    marker = reviewed_marker_sha(pr_number, pr_repo)\n"
        "    if marker is not None:\n"
        "        return True\n"
        "    if marker is None or not marker:\n"
        "        return False"), 1)


def drop_the_sha_from_the_decision_evidence(src: str) -> str:
    """追記記録から sha を落とす = AX②。履歴がどこにも残らなくなる。

    台帳は (PR, 種別) ごとに上書きなので、追記側に sha が無いと「以前は別の sha を
    見ていた」が再現できない。この課題の核心価値 (どの差分を見たかを辿れる) が
    自己矮小化する。
    """
    anchor = ('                         + ([f"sha:{reviewed_sha}"] '
              "if reviewed_sha else [])),")
    assert anchor in src, "anchor not found (drop_the_sha_from_the_decision_evidence)"
    return src.replace(anchor, "                         ),", 1)


def let_two_buckets_disclose_the_same_type(src: str) -> str:
    """開示のバケツの排他を外す = AX③。「実施済み」と「免除」が同時に出る。"""
    anchor = ("    legacy_no_sha = [t for t in legacy_no_sha if t not in satisfied]\n"
              "    _already = set(satisfied) | set(legacy_no_sha)")
    assert anchor in src, "anchor not found (let_two_buckets_disclose_the_same_type)"
    return src.replace(anchor, "    _already = set()", 1)


def accept_any_string_as_a_sha(src: str) -> str:
    """sha の形の検査を外す = AX④。貼り間違いでも成功を名乗る。"""
    anchor = '_SHA_SHAPE = re.compile(r"\\A[0-9a-fA-F]{7,64}\\Z")'
    assert anchor in src, "anchor not found (accept_any_string_as_a_sha)"
    return src.replace(anchor, '_SHA_SHAPE = re.compile(r".*", re.S)', 1)


def hand_write_the_obligations_fake_again(src: str) -> str:
    """試験の fake を本物から導出するのをやめ、手書きに戻す = 保守性②。

    本物のキーが変わっても fake は古い形を返し続け、fake 経由の試験群だけが
    偽の緑になる (judge が反転実験で実測した形)。
    """
    anchor = "    base = review_spine.derive_pr_review_obligations("
    assert anchor in src, "anchor not found (hand_write_the_obligations_fake_again)"
    head = src[: src.index(anchor)]
    tail = src[src.index("    base.update({"):]
    hand = (
        '    base = {"owed": list(_owed), "satisfied": list(_satisfied),\n'
        '            "legacy_no_sha": [], "unpinned": [], "stale": [],\n'
        '            "skipped": [], "head_sha": head_sha,\n'
        '            "head_known": bool(head_sha), "required": required}\n')
    return head + hand + tail


def stop_naming_which_sha_was_reviewed(src: str) -> str:
    """stale の理由から「実施した差分」を落とす = 保守性① で足した次の一手。

    いまの head を言うだけでは「では何を見ればいいのか」が分からない。
    """
    anchor = ('    detail = " / ".join(f"{s[\'review\']}='
              '{s[\'reviewed_sha\'][:12]}" for s in stale)')
    assert anchor in src, "anchor not found (stop_naming_which_sha_was_reviewed)"
    return src.replace(anchor, '    detail = "(省略)"', 1)


def claim_passage_against_the_recorded_sha(src: str) -> str:
    """最後の名乗りを「いまの head」でなく「記録した sha」に対して評価する。

    **ドッグフーディングで自分が踏んだ形** (2026-11-06)。過去の sha を正直に記録
    すると「gate を通過できます」と出るのに、同じ瞬間に `review status` は未実施と
    答えた = 名乗りと実態のズレ。
    """
    anchor = "    _gate = pr_review_obligations(pr_number, pr_repo)\n    _where ="
    assert anchor in src, "anchor not found (claim_passage_against_the_recorded_sha)"
    return src.replace(anchor, (
        "    _gate = pr_review_obligations(pr_number, pr_repo, "
        "head_sha=_reviewed_sha)\n    _where ="), 1)


# --- 独立レビュー 2 周目 (PR #838) の指摘に対する修正への壊し方 -------------

def let_the_gate_recompute_the_verdict(src: str) -> str:
    """関門が `passed` を読むのをやめ、自分で再計算する = AX 2 周目 high の退行。

    判定の所有が 2 箇所に戻るので、新しい呼び出し元が一部を写し忘れる経路が開く。
    """
    anchor = '    if ob.get("passed"):'
    assert anchor in src, "anchor not found (let_the_gate_recompute_the_verdict)"
    return src.replace(anchor, (
        '    if ob.get("pr_state") in ("MERGED", "CLOSED") or '
        '(ob.get("resolved") and not (ob.get("owed") or [])):'), 1)


def make_json_mode_skip_the_verdict(src: str) -> str:
    """`review status --json` が判定より前に return する = judge が実測した形。

    README には「Exits 2 while anything is owed」と書いてあるのに、テキスト
    モードにしか実装されていない状態に戻す。
    """
    anchor = ("    _rc = 0 if ob.get(\"passed\") else 2\n"
              "    if json_mode:\n"
              "        print(json.dumps(ob, ensure_ascii=False))\n"
              "        sys.exit(_rc)")
    assert anchor in src, "anchor not found (make_json_mode_skip_the_verdict)"
    return src.replace(anchor, (
        "    _rc = 0 if ob.get(\"passed\") else 2\n"
        "    if json_mode:\n"
        "        print(json.dumps(ob, ensure_ascii=False))\n"
        "        return"), 1)


def drop_the_merged_short_circuit_from_status(src: str) -> str:
    """`review status` から merge 済みの早期 return を落とす。

    関門は素通りさせるのに status は「未実施、approve / merge は止まります」と
    言い、マージ済み PR にレビューの実行まで促す (judge が実測)。
    """
    anchor = '    if ob.get("pr_state") in ("MERGED", "CLOSED"):\n        print(f"  \u2192 \u3053\u306e PR \u306f\u65e2\u306b'
    assert anchor in src, "anchor not found (drop_the_merged_short_circuit_from_status)"
    idx = src.index(anchor)
    end = src.index("    if not ob.get(\"resolved\"):", idx)
    return src[:idx] + src[end:]


def recompute_passed_in_the_test_stub(src: str) -> str:
    """試験の共有 stub が `passed` を手書きする = fake 経由が偽の緑に戻る。"""
    anchor = '        base["passed"] = commands_shared._review_gate_passed(base)'
    assert anchor in src, "anchor not found (recompute_passed_in_the_test_stub)"
    return src.replace(anchor, '        base["passed"] = not base.get("owed")', 1)


def recompute_the_required_set_in_ci(src: str) -> str:
    """CI の required check が委譲をやめて自分で計算する = 真実源が 2 つに戻る。"""
    anchor = "    return review_spine.required_pr_open_review_types()"
    assert anchor in src, "anchor not found (recompute_the_required_set_in_ci)"
    return src.replace(anchor, (
        '    return [d["review"] for d in '
        'review_spine.batch_review_types_for_node("pr-open")]'), 1)


def inline_the_report_decision_again(src: str) -> str:
    """`review done` の分岐判定を純粋関数から外す (= 戻り値を無視して黙る)。"""
    anchor = "    for _stream, _text in _report[\"lines\"]:"
    assert anchor in src, "anchor not found (inline_the_report_decision_again)"
    end = src.index("    if _report[\"stamp_marker\"]:")
    return src[: src.index(anchor)] + src[end:]


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
    # --- 独立レビュー (PR #838) の指摘に対する修正 ---
    ("促しの抑止を「マーカーが在れば永久」に戻す (AX①)",
     TRIG, suppress_the_nudge_forever_again,
     f"{SHA_T}::test_the_nudge_refires_after_a_commit_is_pushed "
     f"{SHA_T}::test_a_legacy_marker_without_a_sha_does_not_suppress_the_nudge"),

    ("追記記録から sha を落とす (AX② — 履歴がどこにも残らない)",
     CMDS, drop_the_sha_from_the_decision_evidence,
     f"{SHA_T}::test_the_reviewed_sha_survives_in_the_append_only_decision_arm"),

    ("開示のバケツの排他を外す (AX③ — 実施済みと免除が同時に出る)",
     SPINE, let_two_buckets_disclose_the_same_type,
     f"{SHA_T}::test_one_type_is_disclosed_in_exactly_one_bucket"),

    ("sha の形の検査を外す (AX④ — 貼り間違いでも成功を名乗る)",
     SHARED, accept_any_string_as_a_sha,
     f"{SHA_T}::test_the_sha_shape_is_checked"),

    ("試験の fake を手書きに戻す (保守性② — fake 経由が偽の緑に戻る)",
     OBL_STUB, hand_write_the_obligations_fake_again,
     f"{SHA_T}::test_the_shared_obligations_fake_is_derived_from_the_real_function"),

    ("stale の理由から「実施した差分」を落とす (保守性①)",
     SHARED, stop_naming_which_sha_was_reviewed,
     f"{SHA_T}::test_the_gate_messages_are_pure_builders"),
    ("最後の名乗りを「記録した sha」に対して評価する (ドッグフーディングで踏んだ形)",
     CMDS, claim_passage_against_the_recorded_sha,
     f"{SHA_T}::test_review_done_asks_the_real_head_not_the_recorded_sha"),
    # --- 独立レビュー 2 周目 (PR #838) の指摘に対する修正 ---
    ("関門が passed を読まず自分で再計算する (AX 2 周目 high)",
     PR, let_the_gate_recompute_the_verdict,
     f"{SHA_T}::test_the_gate_reads_passed_instead_of_recomputing"),

    ("review status --json が判定より前に return する (出力形式で rc が変わる)",
     CMDS, make_json_mode_skip_the_verdict,
     f"{SHA_T}::test_review_status_exits_the_same_way_in_both_output_modes"),

    ("review status から merge 済みの早期 return を落とす (関門と食い違う)",
     CMDS, drop_the_merged_short_circuit_from_status,
     f"{SHA_T}::test_review_status_and_the_gate_agree_about_a_merged_pr"),

    ("試験の共有 stub が passed を手書きする (fake 経由が偽の緑に戻る)",
     OBL_STUB, recompute_passed_in_the_test_stub,
     f"{SHA_T}::test_the_pass_verdict_lives_in_one_place "
     f"{SHA_T}::test_review_status_and_the_gate_agree_about_a_merged_pr"),

    ("CI の required check が委譲をやめる (真実源が 2 つに戻る)",
     GATE_CI, recompute_the_required_set_in_ci,
     f"{SHA_T}::test_the_ci_required_check_delegates_instead_of_recomputing"),

    ("review done が分岐判定の戻り値を無視する (何も言わなくなる)",
     CMDS, inline_the_report_decision_again,
     f"{SHA_T}::test_review_done_asks_the_real_head_not_the_recorded_sha"),
]


def main() -> int:
    originals = {p: p.read_text(encoding="utf-8")
                 for p in (SPINE, SHARED, PR, CMDS, TRIG, OBL_STUB, GATE_CI)}
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
