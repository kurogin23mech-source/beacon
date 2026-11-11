#!/usr/bin/env python3
"""judge instrument が **逐語で共有している節** がズレないことの照合 (ms-119)。

**何が起きていた** (独立保守性レビュー 6 周目 medium, 2026-11-10): bundle の
``external_references`` にある全貌マップ (application-map) へ「どこまで依ってよいか」
を説明する節が、3 つの instrument (``ax-review`` / ``maintainability-review`` /
``philosophy-review``) の SKILL.md に **一字一句同じ形で** 置かれている。そして
それを同期させる仕掛けは無かった。

皮肉な形で見つかった: この節を 3 箇所に植えたのと **同じコミット** が、まさに
「層をまたいで語彙を 2 回定義すると片方だけ増えて黙ってズレる」ことを直すために
``check-review-mode-vocab.py`` を新設していた。自分が今まさに作っている逐語コピーには
その診断を当てていなかった。

**なぜ共有ファイルに factor out しないのか** (= この検査が存在する理由): instrument は
**文脈ゼロの judge に path で手渡される**。本文を別ファイルへ移して参照 1 行に縮めると、
参照を辿らなかった judge は規則を **黙って失う** —— ドリフトの危険を、より悪い
「判定が規則を知らない」危険に取り替えることになる。instrument が自己完結している
ことは意図された設計なので、**コピーを許したまま、ズレだけを機械で禁じる**。

**この検査が見るもの**: 下の ``SHARED_SECTIONS`` に挙げた見出しの節が、挙げた
instrument すべてで **byte-identical** であること。1 つでも違えば赤。

**この検査が見ないもの**:
- 節の中身が正しいかは見ない (散文の質は機械では測れない)。
- 挙げていない節は見ない。新しく逐語共有する節を増やしたら、ここに足すこと
  (足さなければ、その節は守られない —— だから台帳をこのファイルに置いて、
  「増やしたのに守られていない」を人が気づける 1 箇所にしている)。
- 節が **存在すること** は要求する (見出しが消えていたら赤)。片方だけ節を
  削除して「揃った」と言わせないため。

plain script (pytest 非依存) — ``scripts/ci-strict-drift-guards.sh`` から呼ばれる。
終了コード: 揃っていれば 0、ズレていれば 1。
"""

from __future__ import annotations

import hashlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: (見出し, その節を逐語共有する instrument) の台帳。
#: 見出しは行頭から完全一致で探す (部分一致にすると、別の節の中に同じ語が出た
#: ときに違う場所を測りはじめ、ズレを隠す)。
SHARED_SECTIONS = (
    ("### `external_references` の全貌マップ (application-map) にどこまで依れるか (ms-119 e-4204)",
     ("ax-review", "maintainability-review", "philosophy-review")),
)


def _instrument_path(name: str) -> str:
    return os.path.join(ROOT, "skills", name, "SKILL.md")


def extract_section(text: str, heading: str):
    """``heading`` の行から、次の同レベル以上の見出しの直前までを返す。

    見つからなければ ``None``。**構造で切る** (見出し行の完全一致 → 次の
    ``###``/``##``/``#`` 見出し) ので、節の中の散文に同じ語が出ても測る範囲は動かない。
    """
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.rstrip() == heading:
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        stripped = lines[j]
        if stripped.startswith("#") and not stripped.startswith("####"):
            end = j
            break
    return "\n".join(lines[start:end]).rstrip() + "\n"


def main() -> int:
    bad = 0
    for heading, instruments in SHARED_SECTIONS:
        seen = {}
        missing = []
        for name in instruments:
            path = _instrument_path(name)
            try:
                with open(path, encoding="utf-8") as f:
                    text = f.read()
            except OSError:
                print(f"[instrument-shared-sections] NG: {name} の SKILL.md が "
                      f"読めません ({path})", file=sys.stderr)
                bad += 1
                continue
            section = extract_section(text, heading)
            if section is None:
                missing.append(name)
                continue
            digest = hashlib.sha256(section.encode("utf-8")).hexdigest()
            seen.setdefault(digest, []).append(name)

        if missing:
            print(f"[instrument-shared-sections] NG: 次の instrument に節が "
                  f"在りません: {', '.join(missing)}", file=sys.stderr)
            print(f"  見出し: {heading}", file=sys.stderr)
            print("  片方だけ節を消して「揃った」ことにはできません —— judge は "
                  "instrument を単体で渡されるので、消えた側は規則を知らないまま "
                  "判定します。", file=sys.stderr)
            bad += 1

        if len(seen) > 1:
            print(f"[instrument-shared-sections] NG: 逐語共有の節がズレています",
                  file=sys.stderr)
            print(f"  見出し: {heading}", file=sys.stderr)
            for digest, names in sorted(seen.items(),
                                        key=lambda kv: -len(kv[1])):
                print(f"    {digest[:12]}  {', '.join(sorted(names))}",
                      file=sys.stderr)
            print("  instrument は文脈ゼロの judge に単体で渡されるので、本文を "
                  "共有ファイルへ移して参照にする直し方は採れません (参照を辿らない "
                  "judge が規則を黙って失う)。**揃えて** ください。", file=sys.stderr)
            bad += 1

    if bad:
        return 1

    total = sum(len(i) for _h, i in SHARED_SECTIONS)
    print(f"[instrument-shared-sections] OK: 逐語共有の節 "
          f"{len(SHARED_SECTIONS)} 件が、述べた instrument "
          f"({total} 箇所) すべてで一致しています。")
    print("  **この緑は節の中身が正しいことを主張しません** — 台帳に挙げた節が "
          "byte-identical かだけを見ています (挙げていない節は見ていない。"
          "詳細と限界は docstring)。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
