#!/usr/bin/env python3
"""盤の「自分で畳むか」の既定を、文面が実装と逆に言っていないか (ms-173 e-7265)。

## なぜこの検査が在るか

e-7264 で既定を反転した (手元で開いた盤を 30 分で畳む → 畳まない) とき、Go 側の
定数とその周りは直したが、**利用者が最初に読む 2 箇所が旧既定を宣言したまま残った**:

  * `beacon help --json` の `beacon view` の説明 … "closes after 30m with nobody watching"
  * `viewer/README.md` … 「手元で開いた盤は、誰も見に来なくなって 30 分で自ら畳みます」

独立 AX レビューが実走して見つけた。`check-cli-help-drift.py` はサブコマンドと旗の
**構造**だけを突き合わせ、説明文の内容は見ないので素通りする。

help は文脈ゼロの AI が行動を決める一次情報なので、ここが嘘をつくと
「放っておけば終わる」と誤認して後片付けを省く / 既定と同じ値の旗を「畳まれない
ように」と称して付ける、といった誤りを誘う。盤が死んでいる事故を直した変更の効果
そのものを打ち消す。

## 何を見るか

真実源は Go の `defaultIdleTimeout` (viewer/idle.go) 1 つ。そこから読んだ値と、
文面が主張していることを突き合わせる:

  * 既定が 0 (= 畳まない) なら、文面は「既定で畳む」と読める主張をしてはならない。
  * 既定が 0 でないなら、その値そのものが文面に出ていること (数字が食い違ったまま
    放置されるのを防ぐ)。

**数字の複製を機械で縛る**のが目的で、文章の書き方は縛らない。だから探すのは
「既定で畳む」と読める言い回しだけに絞ってある (広く取ると、経緯を説明する文章
—— 「かつては 30 分で畳んでいた」—— まで赤くして、読み手に history を書けなくする)。

依存なしで走る (`ci-strict-drift-guards.sh` は pytest の無い lint-docs からも呼ばれる)。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

GO_CONST = REPO / "viewer" / "idle.go"
#: 文面を持つ場所。(表示名, パス, その中で見る範囲を絞る正規表現 or None)
CLAIM_SITES = (
    ("beacon help (beacon view の説明)", REPO / "lib" / "commands.py",
     re.compile(r'"command":\s*"beacon view".*?\}', re.S)),
    ("viewer/README.md", REPO / "viewer" / "README.md", None),
)

#: 「既定で畳む」と読める言い回し。**現在形で既定を述べているものだけ**を拾う。
#:
#: 経緯の記述 (「かつては…畳んでいました」「以前は…30 分で畳んでいた」) は拾わない:
#: 過去形・「かつて」「以前」「ました」が同じ文に在れば history と見なす。history を
#: 書けなくすると、なぜ反転したのかが残らなくなる。
CLAIM_PATTERNS = (
    re.compile(r"closes after \d+\s*m", re.I),
    re.compile(r"自ら畳みます"),
    re.compile(r"で自動的に畳みます"),
)
HISTORY_MARKERS = ("かつて", "以前", "畳んでいました", "畳んでいた", "used to ", "previously")


def default_idle_is_never() -> bool:
    """`defaultIdleTimeout` が 0 (= 畳まない) か。読めなければ例外。"""
    text = GO_CONST.read_text(encoding="utf-8")
    m = re.search(r"^const defaultIdleTimeout = (.+)$", text, re.M)
    if not m:
        raise SystemExit(
            f"[idle-default-claims] {GO_CONST.name} に defaultIdleTimeout の宣言が "
            "見つかりません。真実源が動いたなら、この検査の読み方も直してください。")
    return m.group(1).strip() == "0"


def _sentences(text: str):
    for raw in re.split(r"[。\n]", text):
        s = raw.strip()
        if s:
            yield s


def find_claims():
    """「既定で畳む」と現在形で主張している箇所を返す。"""
    hits = []
    for label, path, scope in CLAIM_SITES:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if scope is not None:
            text = "\n".join(m.group(0) for m in scope.finditer(text))
        for sentence in _sentences(text):
            if any(h in sentence for h in HISTORY_MARKERS):
                continue  # 経緯の記述は対象外
            for pat in CLAIM_PATTERNS:
                if pat.search(sentence):
                    hits.append((label, sentence[:160]))
                    break
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strict", action="store_true", help="見つかったら exit 1")
    args = ap.parse_args()

    never = default_idle_is_never()
    claims = find_claims()

    if never and claims:
        print("[idle-default-claims] 既定は「畳まない」なのに、文面が「畳む」と言っています:")
        print()
        for label, sentence in claims:
            print(f"  {label}")
            print(f"    {sentence}")
        print()
        print("  利用者と AI が最初に読む場所なので、ここが嘘をつくと「放っておけば")
        print("  終わる」と誤認して後片付けを省きます。既定を述べている文を直すか、")
        print("  経緯として書く (「以前は…畳んでいました」は対象外です)。")
        return 1 if args.strict else 0

    if not never and not claims:
        print("[idle-default-claims] 既定は「畳む」(defaultIdleTimeout != 0) なのに、"
              "文面がそれを述べていません。既定を変えたなら文面も直してください。")
        return 1 if args.strict else 0

    state = "畳まない" if never else "畳む"
    print(f"[idle-default-claims] OK: 既定 ({state}) と文面が一致しています "
          f"(真実源 = viewer/idle.go の defaultIdleTimeout、"
          f"文面 = beacon help の beacon view 説明 + viewer/README.md)。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
