#!/usr/bin/env python3
"""untrusted_turn.arm() hardcoded-timestamp date-bomb detector (ms-169 e-6305 follow-up).

なぜこれが要るか:
  `untrusted_turn.arm(..., at="2026-09-07T07:00:00Z")` のように **固定の絶対時刻** を
  渡すテストは、時限爆弾になる。arm が書く pending エントリは `_write_all` の
  `_STALE_HOURS` (= 24h) 剪定にかかり、壁時計がその時刻を 24 時間過ぎた瞬間に
  「書いた直後の is_armed が None」で自滅する (= 2026-09-09 に実際に RED 化、#741
  保守性レビューで指摘)。

  テストは「今」を基準にすべき (= `at` を省略すれば arm は `_now_iso()` を使う。
  固定オフセットが要るなら now から相対で計算する)。固定の絶対過去時刻を書くと、
  そのテストは「書いた日から 24h」しか green でいられない。

  このスクリプトは pre-commit / CI で機械的にこのアンチパターンを検知し、
  「arm() に固定絶対時刻を渡さない」を構造的に enforce する (= grep guard)。

検査ルール:
  - tests/ 配下の *.py を走査する。
  - `at=` に 4 桁年で始まる文字列リテラル (= 絶対日付、正規表現 `at\s*=\s*["']20\d\d-\d\d-\d\d`)
    を渡している行を探す。
  - その行が `arm(` 呼び出しの一部 (= 直前 5 行以内に `arm(` がある) なら違反。

許される書き方:
  - `at` を省略する (= arm が現在時刻を使う。多くのテストは armed_at を assert しないので十分)。
  - どうしても固定オフセットが要るなら now から相対計算する
    (例: `work_base.now_iso()` を base に、または「N 時間前」をテスト内で算出)。

実行:
  python3 scripts/check-arm-fixed-timestamp.py            # warn mode (= pre-commit default)
  python3 scripts/check-arm-fixed-timestamp.py --strict   # exit 1 if violation (= CI / gate)
  python3 scripts/check-arm-fixed-timestamp.py <path> ...  # 特定ファイルのみ走査 (= staged files)
"""
from __future__ import annotations

import argparse
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.join(REPO_ROOT, "tests")

# `at=` keyword whose value is a hardcoded absolute date literal (4-digit year).
# The negative lookbehind `(?<!["'])` skips an `at=` that sits INSIDE a string
# literal (e.g. `label = "at=2026-01-01"`) — only a real kwarg (preceded by space
# / comma / paren) matches. `\bat` also never matches inside `created_at` (the
# `_` before `at` is a word char, so there is no word boundary there).
_AT_LITERAL = re.compile(r"""(?<!["'])\bat\s*=\s*["']20\d\d-\d\d-\d\d""")
_ARM_CALL = re.compile(r"\barm\s*\(")
# How many preceding lines to scan for the arm( that this at= belongs to
# (covers multi-line arm calls without a fragile balanced-paren regex).
_LOOKBACK = 5


def _iter_test_files(paths: "list[str]"):
    """Yield *.py files to scan. Explicit paths (= staged files) win; else all of tests/."""
    if paths:
        for p in paths:
            ap = p if os.path.isabs(p) else os.path.join(REPO_ROOT, p)
            if ap.endswith(".py") and os.path.isfile(ap):
                yield ap
        return
    for base, _dirs, files in os.walk(TESTS_DIR):
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(base, f)


def _scan(path: str) -> "list[tuple[int, str]]":
    """Return [(lineno, stripped_line)] where a hardcoded absolute `at=` literal
    sits inside an arm() call."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except Exception:
        return []
    hits = []
    for i, line in enumerate(lines):
        if not _AT_LITERAL.search(line):
            continue
        window = "\n".join(lines[max(0, i - _LOOKBACK):i + 1])
        if _ARM_CALL.search(window):
            hits.append((i + 1, line.strip()))
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="走査する *.py (省略時は tests/ 全体)")
    parser.add_argument(
        "--strict", action="store_true",
        help="違反を検知したら exit 1 (= CI / gate 用)。default は warn のみ。")
    args = parser.parse_args()

    violations = []
    for path in _iter_test_files(args.paths):
        for lineno, snippet in _scan(path):
            rel = os.path.relpath(path, REPO_ROOT)
            violations.append((rel, lineno, snippet))

    if not violations:
        print("[arm-fixed-timestamp] OK: no hardcoded absolute `at=` in arm() test calls")
        return 0

    print(
        "[arm-fixed-timestamp] FAIL: arm() に固定の絶対時刻を渡しているテストがあります。",
        file=sys.stderr)
    print(
        "  この pending は _STALE_HOURS (24h) 剪定で自滅し、その時刻を過ぎるとテストが",
        file=sys.stderr)
    print(
        "  「書いた直後に is_armed が None」で RED 化します (= date-bomb、ms-169 e-6305)。",
        file=sys.stderr)
    print("", file=sys.stderr)
    for rel, lineno, snippet in violations:
        print(f"  - {rel}:{lineno}  {snippet}", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "  直し方: `at=` を省略する (arm は現在時刻を使う。多くのテストは armed_at を",
        file=sys.stderr)
    print(
        "          assert しないので十分)。固定オフセットが要るなら now から相対計算する。",
        file=sys.stderr)

    return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
