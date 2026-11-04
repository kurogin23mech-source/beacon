#!/usr/bin/env python3
"""引退した判断種別のガードが「守る対象を壊したとき実際に赤くなるか」を測る
反転ハーネス (ms-185 e-7320)。

**このガードは一度偽の緑だった。** e-7081 が置いた pin は
``kind in src`` でソース文字列の包含を見ていたので、e-7071 で 3 種別を集合から
外しても「外した」とコメントに書くだけで緑のままだった (独立思想レビューが実測)。
緑は信頼されるので、偽の安全は無ガードより悪い。だから測り方を実体 (辞書の
membership) に直し、**その新しい測り方自身が偽の緑を出さないこと**をここで測る。

ケースは 2 方向ある。片方だけでは足りない:

* **壊したら赤** (``EXPECT_RED``) — 守る性質を壊したとき気づけるか。
* **コメントだけ消したら緑** (``EXPECT_GREEN``) — 測っているのが実体であって
  文字列ではないことの実測。これが赤くなるなら、また文字列を測っている
  (= e-7320 の再発)。test-the-test に当たる唯一のケースなので外さない。

使い方: python3 scripts/invert-decision-vocab-retirement-e7320.py
終了コード: 全ケースが期待どおりなら 0、1 件でも外れるか SKIP なら 1
(SKIP は緑と同じ害なので失敗として数える)。対象ファイルは毎ケース必ず元に戻す。
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import invert_harness as ih  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
VOCAB = ROOT / "lib" / "decision_vocab.py"
CLI = ROOT / "lib" / "cmd_decision.py"

PIN_TEST = "tests/test_trek_record_read_surface_e7081.py"
SCHEMA_TEST = "tests/test_decision_event_schema.py"
COVERAGE_TEST = "tests/test_decision_capture_coverage_ms166.py"
RECOGNITION_TEST = "tests/test_decision_kind_recognition_e6633.py"

RETIRED_PIN = (PIN_TEST + "::test_the_decision_vocabulary_keeps_the_trek_kinds")
NO_TYPO_PIN = (PIN_TEST + "::test_retired_kinds_do_not_trigger_the_typo_warning")


# --- lib/decision_vocab.py への壊し方 ---------------------------------------

def drop_a_retired_kind_from_the_dict(src: str) -> str:
    """引退辞書から trek-review の項目だけを落とす (**コメントは残す**)。

    **これが e-7320 の事故そのもの。** 旧 pin はソース文字列を見ていたので、
    コメントに語が残っているこの状態で緑だった。実体を見ていれば赤くなる。
    """
    anchor = '''    "trek-review":
        "【引退した種別】共同作業空間 (Trek) の leader が配下の作業を検分した判断。"
        "ms-185 で Trek 本体を落としたため、この判断を書く継ぎ目は無くなりました。",
'''
    assert anchor in src, "anchor not found (drop_a_retired_kind_from_the_dict)"
    out = src.replace(anchor, "", 1)
    # コメント側には語が残っていること = 文字列検査では捕まえられない状態を作れている。
    assert "trek-review" in out, \
        "コメントからも語が消えた — 文字列検査でも捕まる変異になってしまっている"
    return out


def empty_the_explanation(src: str) -> str:
    """説明文を空にする (鍵は在るが読み手が種別の意味を解決できない)。

    「辞書に在るか」だけを測っていると素通りする形。鍵だけ在って説明が空 =
    開示の役目を果たさないので、e-7067 の作法 (読み手が過去を読める) が破れる。
    """
    anchor = '''    "halt":
        "【引退した種別】自律で進んでいる作業の綱を引いて止めた判断。ms-185 で Trek "
        "本体を落としたため、この判断を書く継ぎ目は無くなりました。",
'''
    assert anchor in src, "anchor not found (empty_the_explanation)"
    return src.replace(anchor, '    "halt": "",\n', 1)


def promote_a_retired_kind_back_into_known(src: str) -> str:
    """引退種別を産出者台帳の母集団 (KNOWN) に戻す。

    産出する継ぎ目が無いので捕獲被覆の検査が永久に赤くなる状態。e-7070 がこれを
    避けるために外したのであって、「読めるように戻す」の答えが KNOWN への復帰では
    ないことを固定する (答えは引退辞書 = 読み手だけの面)。
    """
    anchor = '        "dm-send", "scope-approval",'
    assert anchor in src, "anchor not found (promote_a_retired_kind_back_into_known)"
    return src.replace(anchor, '        "dm-send", "scope-approval", "trek-review",', 1)


def strip_the_kind_names_from_comments(src: str) -> str:
    """**コメントからだけ** 3 種別の名前を消す (辞書は無傷)。

    期待は **緑**。これが赤くなるなら、測っているのは実体ではなく文字列であり、
    e-7320 と同じ偽の緑を作れる状態に戻っている。
    """
    anchor = ('        # ms-90 Trek 由来の経路。ms-185 e-7070 で "trek-review" / "halt" /\n'
              '        # "resume" の 3 つを外した: 判断の継ぎ目 (leader のレビュー / 綱を引く /\n'
              '        # 戻す) と builder ごと消えたので、**産出できない種別**になった。\n')
    assert anchor in src, "anchor not found (strip_the_kind_names_from_comments)"
    out = src.replace(
        anchor,
        '        # ms-90 Trek 由来の経路。ms-185 e-7070 で 3 つを外した: 判断の継ぎ目\n'
        '        # と builder ごと消えたので、**産出できない種別**になった。\n', 1)
    # 残った出現が辞書の鍵だけであることを確かめる (= コメント由来の緑に戻れない形)。
    for kind in ("trek-review", "halt", "resume"):
        assert out.count(f'"{kind}"') == 1, (
            f"{kind} の出現が 1 箇所 (辞書の鍵) になっていない — "
            f"コメントが残っていると test-the-test にならない")
    return out


# --- lib/cmd_decision.py への壊し方 -----------------------------------------

def drop_retired_from_the_recognized_set(src: str) -> str:
    """読み側の結合から引退辞書を外す (= 偽の「綴り違い」警告が戻る)。

    e-7081 の判断が守りたかった実害そのもの。辞書に在っても、読み手の集合へ
    流していなければ ``--kind trek-review`` は綴りを疑わされる。
    「足した」≠「全域に効く」の分かれ目はここ。
    """
    anchor = "                     | set(_RETIRED_KINDS))"
    assert anchor in src, "anchor not found (drop_retired_from_the_recognized_set)"
    return src.replace(anchor, "                     )", 1)


EXPECT_RED = [
    # (ラベル, 対象ファイル, 壊し方, 赤くなるべきテスト)
    ("引退辞書から項目を落とす (コメントは残す = e-7320 の事故そのもの)",
     VOCAB, drop_a_retired_kind_from_the_dict, RETIRED_PIN),
    ("説明文を空にする (鍵だけ在って意味が解決できない)",
     VOCAB, empty_the_explanation, RETIRED_PIN),
    ("引退種別を KNOWN に戻す (= 被覆の検査が永久に赤くなる形)",
     VOCAB, promote_a_retired_kind_back_into_known, RETIRED_PIN),
    ("引退種別を KNOWN に戻す → 捕獲被覆の検査も赤くなる",
     VOCAB, promote_a_retired_kind_back_into_known,
     COVERAGE_TEST + "::test_decision_capture_covers_known_kinds"),
    ("引退種別を KNOWN に戻す → 「外れていること」の固定も赤くなる",
     VOCAB, promote_a_retired_kind_back_into_known,
     SCHEMA_TEST + "::test_known_kinds_documented_but_vocabulary_open"),
    ("読み側の結合から引退辞書を外す (偽の綴り違い警告が戻る)",
     CLI, drop_retired_from_the_recognized_set, NO_TYPO_PIN),
    ("読み側の結合から引退辞書を外す → コードが書く種別の被覆も赤くなる",
     CLI, drop_retired_from_the_recognized_set,
     RECOGNITION_TEST + "::test_recognized_set_consumes_the_ledger_not_only_the_vocabulary"),
]

EXPECT_GREEN = [
    # 測っているのが実体であって文字列ではないことの実測 (test-the-test)。
    ("コメントからだけ種別名を消す (辞書は無傷) → 緑のままであるべき",
     VOCAB, strip_the_kind_names_from_comments, RETIRED_PIN + " " + NO_TYPO_PIN),
]

BASELINE = [PIN_TEST, SCHEMA_TEST, COVERAGE_TEST, RECOGNITION_TEST]


def main() -> int:
    originals = {p: p.read_text(encoding="utf-8") for p in (VOCAB, CLI)}
    if not ih.check_baseline(BASELINE, ROOT):
        return 1

    bad = 0
    for cases, want_red in ((EXPECT_RED, True), (EXPECT_GREEN, False)):
        for label, path, mutate, target in cases:
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
            ok = (went_red == want_red)
            print(("  OK  " if ok else " MISS ") + label)
            print("          → %s" % tail)
            if not ok:
                bad += 1
                print("          期待: %s / 実際: %s"
                      % ("赤" if want_red else "緑", "赤" if went_red else "緑"))

    print()
    if bad:
        print("NG: %d 件が期待と違うか SKIP。偽の安全なので直すこと。" % bad)
        return 1
    if not ih.check_not_polluted(BASELINE, ROOT):
        return 1
    print()
    print("OK: %d 件すべて期待どおり (赤 %d / 緑 %d)。"
          % (len(EXPECT_RED) + len(EXPECT_GREEN), len(EXPECT_RED), len(EXPECT_GREEN)))
    return 0


if __name__ == "__main__":
    sys.exit(ih.guarded_main(main, sys.argv, __doc__, ROOT))
