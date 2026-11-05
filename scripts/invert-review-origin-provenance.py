#!/usr/bin/env python3
"""原典の出所を守るピンが「壊したとき実際に赤くなるか」を測る反転ハーネス (ms-119 e-7345)。

緑のガードは信頼されるので、**偽の安全は無ガードより悪い**。このピンが守るのは
「レビューが、使っていない基準で判定したことを隠せない」という性質なので、壊したら
赤くなることを実測しないと、ピンそれ自体が偽の安心になる。

当てる壊し方は、**実際に起きていた形そのもの** を含む: 原典を capability の作業ツリー
から解決する (= 修正前の挙動)。これが赤くならなければ、このピンは回帰を捕まえない。

使い方: python3 scripts/invert-review-origin-provenance.py
終了コード: 全ケースで期待どおり赤くなれば 0、1 件でも「壊したのに緑」か SKIP なら 1
(SKIP は緑と同じ害なので失敗として数える)。対象ファイルは毎ケース必ず元に戻す。
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import invert_harness as ih  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
CMD = ROOT / "lib" / "commands.py"
SPINE = ROOT / "lib" / "review_spine.py"
T = "tests/test_review_origin_provenance_e7345.py"


def resolve_from_capability_again(src: str) -> str:
    """原典を capability 側から解決する形に戻す (= 修正前の挙動そのもの)。

    **これが本番の事故。** worktree で走らせても main の作業ツリーの原典が配られ、
    main が未 pull ならさらに古い版が配られる。
    """
    anchor = "    reviewed_text = _read(reviewed_path) if reviewed_path else None"
    assert anchor in src, "anchor not found (resolve_from_capability_again)"
    return src.replace(anchor, "    reviewed_text = None", 1)


def stop_disclosing_the_divergence(src: str) -> str:
    """食い違いを黙って飲む (= 勝者を静かに選ぶ)。

    内容は正しい方を配るので、出力だけ見れば問題なく見える。**使っていない基準が
    在ったことが隠れる**のが害。
    """
    anchor = '            gaps.append(\n                f"原典 {ref} が 2 つの作業ツリーで食い違っています'
    assert anchor in src, "anchor not found (stop_disclosing_the_divergence)"
    return src.replace(anchor, '            pass  # MUTANT\n            _unused = (\n                f"原典 {ref} が 2 つの作業ツリーで食い違っています', 1)


def warn_even_when_the_trees_agree(src: str) -> str:
    """食い違っていなくても警告する (= 常に出る警告は読まれなくなる)。"""
    anchor = "        if cap_text is not None and cap_text != reviewed_text:"
    assert anchor in src, "anchor not found (warn_even_when_the_trees_agree)"
    return src.replace(anchor, "        if cap_text is not None:", 1)


def drop_the_capability_fallback(src: str) -> str:
    """対象リポに無いとき capability に落ちなくする (= beacon 以外のレビューが壊れる)。"""
    anchor = "    elif cap_text is not None:"
    assert anchor in src, "anchor not found (drop_the_capability_fallback)"
    return src.replace(anchor, "    elif False:", 1)


def hide_where_it_looked(src: str) -> str:
    """見つからないときに探した場所を言わない (= 回復経路が消える / P6)。"""
    anchor = ('              f"(探した場所: {reviewed_path or \'(git 管理下でない)\'} / {cap_path})",')
    assert anchor in src, "anchor not found (hide_where_it_looked)"
    return src.replace(anchor, '              "",', 1)


def stop_carrying_provenance(src: str) -> str:
    """bundle が出所を運ばなくする (= どの版で判定したか後から分からない)。"""
    anchor = "    if provenance:\n        slot.update({k: v for k, v in provenance.items() if v})"
    assert anchor in src, "anchor not found (stop_carrying_provenance)"
    return src.replace(anchor, "    if False:\n        slot.update({})", 1)


CASES = [
    ("原典を capability 側から解決する形に戻す (= 本番の事故そのもの)", CMD,
     resolve_from_capability_again,
     T + "::test_the_reviewed_repo_wins_and_the_divergence_is_disclosed"),
    ("食い違いを黙って飲む", CMD, stop_disclosing_the_divergence,
     T + "::test_the_reviewed_repo_wins_and_the_divergence_is_disclosed"),
    ("食い違っていなくても警告する", CMD, warn_even_when_the_trees_agree,
     T + "::test_no_gap_when_the_two_trees_agree"),
    ("capability への fallback を外す", CMD, drop_the_capability_fallback,
     T + "::test_falls_back_to_the_capability_for_a_repo_that_is_not_this_tool"),
    ("見つからないときに探した場所を言わない", CMD, hide_where_it_looked,
     T + "::test_missing_in_both_trees_exits_naming_where_it_looked"),
    ("bundle が出所を運ばなくする", SPINE, stop_carrying_provenance,
     T + "::test_the_bundle_carries_which_tree_the_origin_came_from"),
]


def main() -> int:
    originals = {p: p.read_text(encoding="utf-8") for p in (CMD, SPINE)}
    if not ih.check_baseline([T], ROOT):
        return 1
    bad = 0
    for label, path, mutate, target in CASES:
        try:
            path.write_text(mutate(originals[path]), encoding="utf-8")
            ih.invalidate_bytecode_cache(path, ROOT)
            went_red, tail = ih.run_and_classify(target, ROOT)
        except AssertionError as exc:
            ih.restore(originals, ROOT)
            print("  SKIP  %s: 当てる場所が無い (%s) — SKIP は緑と同じ害なので、"
                  "壊し方を実装に合わせて直すこと" % (label, exc))
            bad += 1
            continue
        finally:
            ih.restore(originals, ROOT)
        print(("  OK  " if went_red else " MISS ") + label + "  -> " + tail)
        if not went_red:
            bad += 1
    print()
    if bad:
        print("NG: %d 件が「壊したのに赤くならない」か SKIP。偽の安全なので直すこと。" % bad)
        return 1
    if not ih.check_not_polluted([T], ROOT):
        return 1
    print("OK: %d 件すべて、守る対象を壊すと赤くなった。" % len(CASES))
    return 0


if __name__ == "__main__":
    sys.exit(ih.guarded_main(main, sys.argv, __doc__, ROOT))
