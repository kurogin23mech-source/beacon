#!/usr/bin/env python3
"""AX レビューの計器を守るピンが「壊したとき実際に赤くなるか」を測る反転ハーネス (ms-119)。

緑のガードは信頼されるので、**偽の安全は無ガードより悪い**。独立レビューが実測した
とおり、この計器 (原典 + harness) は長らく **何も固定されていなかった** —— 原則を
1 つ丸ごと消しても、必須の出力キーを消しても、どのテストも赤くならなかった。
``tests/test_ax_review_instrument_e7340.py`` でそれを固定したので、その固定自身が
効いていることをここで測る。

**初版のピンは 1 件 false-pass した。** 重さの語彙を定義表から 1 行消しても、同じ語が
原典の別の節にも現れるため ``name in src`` が満たされ、緑のままだった。定義の在りかを
構造で特定して集合比較する形に直してある —— その違いをこのハーネスが捕まえた。

使い方: python3 scripts/invert-ax-instrument-pins.py
終了コード: 全ケースで期待どおり赤くなれば 0、1 件でも「壊したのに緑」か SKIP なら 1
(SKIP は緑と同じ害なので失敗として数える)。対象ファイルは毎ケース必ず元に戻す。
"""
from __future__ import annotations

import pathlib, sys
W = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import invert_harness as ih

ORIGIN = W / "skills/ax-review/principles.md"
HARNESS = W / "skills/ax-review/SKILL.md"
T = "tests/test_ax_review_instrument_e7340.py"

orig = {ORIGIN: ORIGIN.read_text(encoding="utf-8"),
        HARNESS: HARNESS.read_text(encoding="utf-8")}

def drop_p4(s):
    a = "### P4 同じ操作を 2 回呼んでも壊れない"
    assert a in s
    return s.replace(a, "### 二回呼んでも壊れない", 1)

def drop_failure_class(s):
    return s.replace('"failure_class"', '"failure_kind"')

def rename_severity(s):
    return s.replace('"severity"', '"impact"')

def drop_naming_token(s):
    a = "(レビュー出力では `naming`)"
    assert a in s
    return s.replace(a, "", 1)

def drop_basis(s):
    assert "basis" in s
    return s.replace("basis", "rationale")

def drop_weight_vocab(s):
    a = "| **顧客に届く** |"
    assert a in s
    return s.replace(a, "| **外に出る** |", 1)

def drop_failure_class_row(s):
    a = "| 指せない |"
    assert a in s
    return s.replace(a, "| 指し示せない |", 1)

CASES = [
    ("原典から P4 の見出しを消す", ORIGIN, drop_p4, T + "::test_origin_declares_all_eight_principles"),
    ("原典から naming トークンを消す", ORIGIN, drop_naming_token,
     T + "::test_origin_declares_the_two_cross_cutting_lenses_with_their_tokens"),
    ("原典から重さの語彙を 1 つ消す", ORIGIN, drop_weight_vocab, T + "::test_origin_declares_the_three_weights"),
    ("原典から失敗の分類を 1 つ消す", ORIGIN, drop_failure_class_row, T + "::test_origin_declares_the_four_failure_classes"),
    ("harness から failure_class を消す", HARNESS, drop_failure_class, T + "::test_harness_requires_every_finding_key"),
    ("harness の severity を別名にする (= 初版の誤り)", HARNESS, rename_severity,
     T + "::test_the_weight_key_is_the_one_the_fold_reads"),
    ("harness から basis を消す", HARNESS, drop_basis, T + "::test_harness_requires_the_per_principle_verdict_map"),
]

def main() -> int:
    if not ih.check_baseline([T], W):
        return 1
    bad = 0
    for label, path, mut, target in CASES:
        try:
            path.write_text(mut(orig[path]), encoding="utf-8")
            ih.invalidate_bytecode_cache(path, W)
            red, tail = ih.run_and_classify(target, W)
        except AssertionError as exc:
            ih.restore(orig, W)
            print("  SKIP  %s: 当てる場所が無い (%s) — SKIP は緑と同じ害なので、"
                  "壊し方を実装に合わせて直すこと" % (label, exc))
            bad += 1
            continue
        finally:
            ih.restore(orig, W)
        print(("  OK  " if red else " MISS ") + label + "  -> " + tail)
        if not red:
            bad += 1
    print()
    if bad:
        print("NG: %d 件が「壊したのに赤くならない」か SKIP。偽の安全なので直すこと。" % bad)
        return 1
    if not ih.check_not_polluted([T], W):
        return 1
    print("OK: %d 件すべて、守る対象を壊すと赤くなった。" % len(CASES))
    return 0


if __name__ == "__main__":
    sys.exit(ih.guarded_main(main, sys.argv, __doc__, W))
