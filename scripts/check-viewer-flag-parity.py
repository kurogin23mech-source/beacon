#!/usr/bin/env python3
"""`beacon view` が Go の盤に渡す旗が、Go 側に実在するかを照合する (ms-170 e-7147)。

**なぜ要るのか**: 旗の名前は 4 箇所に手で書かれている ——
Go の `flag.*` 定義 / bash の `bin/beacon` / Python の `beacon_cli/dispatch.py` /
help 台帳 (`lib/commands.py`)。既存の `check-cli-help-drift.py` は後ろの 3 つを
突き合わせるが、**Go 側は見ていない**。だから Go で旗名を変えても (例
`--no-reuse` → `--force-new`) 既存のガードは緑のままで、壊れるのは
`beacon view --no-reuse` を実際に叩いた瞬間 —— `go test` にも pytest にも出ない。
独立レビュー (PR#829 保守性 medium) がこの穴を指摘した。

**何を見るか**: `lib/cmd_view.py` の `viewer_argv` が組み立てうる旗が、
`viewer/main.go` の `flag.*` 定義に在ること。向きはこれだけでよい ——
Go だけが持つ旗 (将来の内部用) は渡さなければ害が無いが、Python が渡す旗が
Go に無いと **必ず** parse error で落ちる。

**なぜ構文木で見るか**: 文字列の部分一致だと、コメントや docstring に出てくる
旗名を「定義されている」と読んでしまう。緩い一致でこのリポジトリは何度も
偽の緑を作っている (CORE: 緑のガードは信頼されるので偽の安全は無ガードより悪い)。
Go は Python の ast で読めないので、`flag.<型>("<名前>"` の形を正規表現で
**定義の形に限って**取る (コメント行は除く)。Python 側は ast で読む。

使い方:
    python3 scripts/check-viewer-flag-parity.py          # 不一致なら非 0
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
GO_MAIN = ROOT / "viewer" / "main.go"
PY_VIEW = ROOT / "lib" / "cmd_view.py"

#: Go の旗定義。`flag.Bool("no-reuse", ...)` の形だけを取る。
_GO_FLAG = re.compile(r'\bflag\.(?:String|Int|Bool|Duration|Float64)\(\s*"([^"]+)"')


def go_flags(source: str) -> set[str]:
    """Go が受け付ける旗の名前 (コメント行は数えない)。"""
    names: set[str] = set()
    for line in source.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("//"):
            continue
        names.update(_GO_FLAG.findall(line))
    return names


def python_passed_flags(source: str) -> set[str]:
    """``viewer_argv`` が argv に載せうる ``--`` 付きの旗 (構文木で読む)。

    `argv.append("--expose")` / `argv.extend(["--port", str(port)])` の両方を
    拾う。関数の中だけを見るので、docstring やコメントの旗名は入らない
    (= 書いてあるだけの名前を「渡している」と読まない)。
    """
    tree = ast.parse(source)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "viewer_argv"), None)
    if fn is None:
        raise SystemExit(
            "Error: lib/cmd_view.py に viewer_argv が見つかりません。"
            "旗の写像の持ち主が変わったなら、このガードの見る先も直してください")
    names: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.startswith("--"):
                names.add(node.value[2:])
    return names


def main() -> int:
    go = go_flags(GO_MAIN.read_text(encoding="utf-8"))
    passed = python_passed_flags(PY_VIEW.read_text(encoding="utf-8"))
    if not go:
        print("Error: viewer/main.go から旗を 1 つも読めませんでした "
              "(定義の書き方が変わった?)。このガードが空回りしています",
              file=sys.stderr)
        return 2
    missing = sorted(passed - go)
    if missing:
        print("[viewer-flag-parity] `beacon view` が渡す旗が Go 側にありません:",
              file=sys.stderr)
        for name in missing:
            print(f"  --{name}  (lib/cmd_view.py の viewer_argv が渡すが "
                  f"viewer/main.go に flag 定義が無い)", file=sys.stderr)
        print(file=sys.stderr)
        print("  このままだと `beacon view` は Go の parse error で落ちます。",
              file=sys.stderr)
        print("  go test / pytest には出ないので、ここで止めています。",
              file=sys.stderr)
        return 1
    print(f"[viewer-flag-parity] OK: `beacon view` が渡す旗 {len(passed)} 件は"
          f"すべて Go 側 ({len(go)} 件) に実在します "
          f"(渡す: {', '.join('--' + n for n in sorted(passed))})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
