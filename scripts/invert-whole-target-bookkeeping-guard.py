#!/usr/bin/env python3
"""ms-119 e-7352: target 全体の収集が作業記録を落とすガードを、壊して赤を確かめる。

緑は信頼されるので、偽の安全は無ガードより悪い。この harness は
「赤くなるべき変異」と「緑のままであるべき変異」を**両方**入れて、検査が
形だけ通る状態 (= 契約を保った変異で赤くなる = 過剰) と、
穴を見逃す状態 (= 契約を破る変異で緑 = 不足) の両方を捕まえる。

使い方: python3 -B scripts/invert-whole-target-bookkeeping-guard.py
(``-B`` は必須。stale な .pyc が偽の緑/赤を作る。)
"""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TARGET = ROOT / "lib" / "commands.py"
TESTS = [
    "tests/test_review_bundle_bookkeeping_e7352.py",
    "tests/test_review_whole_target_artifact.py",
]

# (名前, 置換前, 置換後, 赤くなるべきか)
MUTATIONS = [
    ("落とす処理を外す",
     "        diff, dropped_here = _rs.strip_bookkeeping_from_diff(diff)\n"
     "        if dropped_here:\n"
     "            dropped_all.extend(dropped_here)\n",
     "        dropped_here = []\n",
     True),
    ("落としたことの開示を外す",
     "    bookkeeping_gap = _rs.bookkeeping_drop_gap(dropped_all)\n"
     "    if bookkeeping_gap:\n",
     "    bookkeeping_gap = \"\"\n"
     "    if bookkeeping_gap:\n",
     True),
    ("作業記録だけの commit の理由を『変更なし』に潰す",
     '                "bookkeeping-only (この commit は Beacon の作業記録しか変えていない)"\n'
     "                if dropped_here else\n",
     '                "no-textual-diff (空 / メタのみの commit の可能性)"\n'
     "                if dropped_here else\n",
     True),
    # 契約を保った変異 — 緑のままが正しい。赤くなるなら検査が形を測っている。
    ("中身を変えずに局所変数名だけ変える (契約は保たれる)",
     "        diff, dropped_here = _rs.strip_bookkeeping_from_diff(diff)\n"
     "        if dropped_here:\n"
     "            dropped_all.extend(dropped_here)\n",
     "        diff, _dh = _rs.strip_bookkeeping_from_diff(diff)\n"
     "        if _dh:\n"
     "            dropped_all.extend(_dh)\n"
     "        dropped_here = _dh\n",
     False),
]


def run_tests():
    """(rc, 末尾の要約行) — パイプを挟まずに rc を測る。"""
    p = subprocess.run([sys.executable, "-B", "-m", "pytest", *TESTS, "-q"],
                       cwd=ROOT, capture_output=True, text=True)
    tail = [L for L in (p.stdout or "").splitlines() if "passed" in L or "failed" in L]
    return p.returncode, (tail[-1] if tail else "(要約行なし)")


def main():
    original = TARGET.read_text(encoding="utf-8")
    rc, summary = run_tests()
    if rc != 0:
        print(f"BASELINE が赤い: {summary}\n  変異を測る前に直すこと。")
        return 1
    print(f"baseline: 緑 ({summary})")

    bad = []
    try:
        for name, before, after, should_fail in MUTATIONS:
            if original.count(before) != 1:
                bad.append(f"{name}: 置換前の文字列が {original.count(before)} 回 — "
                           f"harness が実装に追随していない (測れていない)")
                continue
            TARGET.write_text(original.replace(before, after), encoding="utf-8")
            rc, summary = run_tests()
            got_red = rc != 0
            mark = "OK" if got_red == should_fail else "NG"
            want = "赤" if should_fail else "緑"
            print(f"  [{mark}] {name}: 期待={want} 実測={'赤' if got_red else '緑'} ({summary})")
            if got_red != should_fail:
                bad.append(f"{name}: 期待={want} だが実測={'赤' if got_red else '緑'}")
    finally:
        TARGET.write_text(original, encoding="utf-8")

    rc, summary = run_tests()
    if rc != 0:
        bad.append(f"復元後も赤い: {summary}")
    else:
        print(f"復元後: 緑 ({summary})")

    if bad:
        print("\n不合格:")
        for b in bad:
            print(f"  - {b}")
        return 1
    print("\n全件合格 — ガードは契約違反で赤くなり、契約を保った変異では緑のまま")
    return 0


if __name__ == "__main__":
    sys.exit(main())
