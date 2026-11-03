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
  * 既定が 0 でないなら、**その値そのものが文面に出ていること**。`30 * time.Minute`
    なら `30m` / `30 分` のどちらかが claim 句に現れることを要求する (数字が食い違った
    まま放置されるのを防ぐ)。
  * **定数がリテラルでなく読めないときは「判定不能」で中立に終わる** (exit 0)。
    読めなかったことと「食い違っていない」を同じ結果に畳むと、等価なリファクタ
    (`time.Duration(0)` 等) で「既定を変えたなら文面も直して」という嘘の診断が出る
    —— 独立レビューが実測した誤診で、存在しない回帰を追わせる。

**数字の複製を機械で縛る**のが目的で、文章の書き方は縛らない。

## 検査しないこと (= 緑が主張しない範囲)

**この緑は「idle 既定についての矛盾が repo のどこにも無い」ことを意味しない。** 見るのは
`CLAIM_SITES` に挙げた場所だけで、言い回しも `CLAIM_PATTERNS` に挙げた形だけ。独立
レビューが実測した通り、別の言い回し (「しばらく操作が無いと盤は停止します」) や
挙げていない 4 箇所目のファイルは素通りする。だから緑のメッセージには**見た範囲を
そのまま書く** (広い全称命題として読まれないように)。

`CLAIM_PATTERNS` を広げるときは `tests/test_idle_default_claims_guard.py` が
「拾うべき文 / 免除すべき文」を両方固定しているので、そこに例を足してから広げる。

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
    # help は **description の文だけ** を見る (e-7266)。entry 全体を範囲にすると
    # `"flags": [..., "--idle-timeout <30m|2h|0>"]` の値の例示が「30m で畳む」と
    # 読まれて誤検知する (広げた直後に実測した)。旗の例示は散文ではないので、
    # 範囲の側で外すほうが、言い回しの除外規則を増やすより確か。
    ("beacon help (beacon view の説明)", REPO / "lib" / "commands.py",
     re.compile(r'"command":\s*"beacon view".*?"description":\s*"([^"]*)"', re.S)),
    ("viewer/README.md", REPO / "viewer" / "README.md", None),
    # 3 箇所目 (e-7266)。`beacon view` の入口の docstring で、環境変数の説明として
    # 既定を述べる。**e-7264 で既定を反転したとき、ここにも新しい既定を書いた**のに
    # 検査の対象に入れ忘れていた —— 独立レビューが「自分が書いた場所が走査外」と
    # 実測して指摘した。文面を持つ場所を足したら、ここにも足すこと。
    ("lib/cmd_view.py (beacon view の docstring)", REPO / "lib" / "cmd_view.py", None),
)

#: 「既定で畳む」と読める言い回し。**現在形で既定を述べているものだけ**を拾う。
#:
#: 経緯の記述 (「かつては…畳んでいました」「以前は…30 分で畳んでいた」) は拾わない:
#: 過去形・「かつて」「以前」「ました」が同じ文に在れば history と見なす。history を
#: 書けなくすると、なぜ反転したのかが残らなくなる。
#: 「既定で畳む」と読める言い回し。**現在形で既定を述べているものだけ**を拾う。
#:
#: 独立レビューが素通りさせた 3 形を足した (e-7266): 「しばらく操作が無いと盤は
#: 停止します」/「既定では N 分後に盤を終了します」/「By default the board shuts
#: down after N minutes idle」。決め打ちの文を並べるのをやめ、**畳む動作の語** と
#: **既定/時間の語** が同じ文に在ることを条件にした (語の組み合わせで見るほうが、
#: 言い回しの揺れに強い)。
CLOSE_VERBS = (
    re.compile(r"自ら畳みます|自動的に畳みます|畳みます|停止します|終了します"),
    re.compile(r"closes?\b|shuts? down|self-close", re.I),
)
DEFAULT_OR_TIME = (
    # 時間の綴りは分だけでなく時/秒も (`2h` が素通りした — 自分の試験表で捕まえた)。
    re.compile(r"既定|デフォルト|\d+\s*分|\d+\s*時間|\d+\s*[mhs]\b|しばらく"),
    re.compile(r"by default|after \d+|\d+\s*minutes?", re.I),
)
CLAIM_PATTERNS = ()  # 互換のため残す (いまは語の組み合わせで判定する)
HISTORY_MARKERS = ("かつて", "以前", "畳んでいました", "畳んでいた", "used to ", "previously")


def default_idle() -> "tuple[bool | None, str]":
    """`(畳まないか, 生の式)` を返す。読めないときは ``(None, 式)``。

    **読めないことを「食い違っていない」に畳まない** (e-7266)。`0` と等価な
    `time.Duration(0)` に書き換えただけで「既定を変えたなら文面も直して」という
    嘘の診断が出ていた (独立レビューが実測)。判定不能は第三の状態として扱う。
    """
    text = GO_CONST.read_text(encoding="utf-8")
    m = re.search(r"^const defaultIdleTimeout = (.+)$", text, re.M)
    if not m:
        raise SystemExit(
            f"[idle-default-claims] {GO_CONST.name} に defaultIdleTimeout の宣言が "
            "見つかりません。真実源が動いたなら、この検査の読み方も直してください。")
    expr = m.group(1).split("//")[0].strip()
    if expr == "0":
        return True, expr
    # `30 * time.Minute` / `2 * time.Hour` のような素直な形だけ読む。
    mm = re.fullmatch(r"(\d+)\s*\*\s*time\.(Minute|Hour|Second)", expr)
    if mm:
        return False, expr
    return None, expr


def expected_tokens(expr: str):
    """既定が 0 でないとき、文面に現れるべき綴りの候補。"""
    mm = re.fullmatch(r"(\d+)\s*\*\s*time\.(Minute|Hour|Second)", expr)
    if not mm:
        return []
    n, unit = mm.group(1), mm.group(2)
    suffix = {"Minute": ("m", "分"), "Hour": ("h", "時間"), "Second": ("s", "秒")}[unit]
    return [f"{n}{suffix[0]}", f"{n} {suffix[1]}", f"{n}{suffix[1]}"]


#: 旗の **値の例示** (`--idle-timeout <30m|2h|0>` / `(例 2h)`)。時間の語に見えるが
#: 既定の主張ではない。これを数えると、旗を説明しただけで赤くなる (実測: 広げた
#: 直後に `beacon help` の flags 配列がこれで誤検知した)。
FLAG_EXAMPLE_RE = re.compile(r"--idle-timeout\s*[<(\[]|例\s*[`'\"]?\d+[mhs]")


def _claims_it_closes(sentence: str) -> bool:
    """この文は「既定で畳む」と現在形で述べているか (純関数)。

    畳む動作の語と、既定/時間の語の **両方** が同じ文に在ることを条件にする。
    片方だけで拾うと「盤を終了するには Ctrl+C」のような操作説明まで赤くなる。

    旗の値の例示は時間の語から除く (FLAG_EXAMPLE_RE)。`--idle-timeout <30m|2h|0>`
    は「30m で畳む」と言っているのではなく、受け付ける値を並べているだけ。
    """
    closes = any(p.search(sentence) for p in CLOSE_VERBS)
    if not closes:
        return False
    cleaned = FLAG_EXAMPLE_RE.sub(" ", sentence)
    return any(p.search(cleaned) for p in DEFAULT_OR_TIME)


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
            # 捕獲群があればそれだけを見る (= 範囲をさらに絞れる)。
            text = "\n".join(
                (m.group(1) if m.groups() else m.group(0))
                for m in scope.finditer(text))
        for sentence in _sentences(text):
            if any(h in sentence for h in HISTORY_MARKERS):
                continue  # 経緯の記述は対象外
            if _claims_it_closes(sentence):
                hits.append((label, sentence[:160]))
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strict", action="store_true", help="見つかったら exit 1")
    args = ap.parse_args()

    never, expr = default_idle()
    claims = find_claims()
    scanned = ", ".join(label for label, _, _ in CLAIM_SITES)

    if never is None:
        # 判定不能。黙って緑にせず、読めなかったことを名乗って中立に終わる。
        print(f"[idle-default-claims] 判定不能: defaultIdleTimeout = {expr!r} を"
              "値として読めませんでした。")
        print("  この検査は `0` と `N * time.Minute|Hour|Second` の形だけを読みます。")
        print("  形を変えたなら、この検査の読み方も合わせて直してください "
              "(読めないことを「食い違いなし」に畳まないため、ここでは失敗させません)。")
        return 0

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

    if not never:
        want = expected_tokens(expr)
        if not claims:
            print(f"[idle-default-claims] 既定は「畳む」({expr}) なのに、文面がそれを"
                  "述べていません。既定を変えたなら文面も直してください。")
            return 1 if args.strict else 0
        # 値そのものが文面に出ているか (docstring が宣言していた契約、e-7266)。
        if want and not any(any(w in sentence for w in want) for _, sentence in claims):
            print(f"[idle-default-claims] 既定は {expr} ですが、文面にその値 "
                  f"({' / '.join(want)}) が見当たりません:")
            print()
            for label, sentence in claims:
                print(f"  {label}")
                print(f"    {sentence}")
            print()
            print("  数字が食い違ったまま放置されるのを防ぐための検査です。")
            return 1 if args.strict else 0

    state = "畳まない" if never else f"畳む ({expr})"
    print(f"[idle-default-claims] OK: 既定 ({state}) と文面が一致しています。")
    print(f"  見た範囲: {scanned}")
    print("  **この緑は repo 全体の無矛盾を主張しません** — 上の場所の、"
          "畳む動作の語と既定/時間の語が同じ文に在る形だけを見ています "
          "(詳細と限界は docstring の「検査しないこと」)。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
