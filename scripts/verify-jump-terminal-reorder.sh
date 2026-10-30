#!/bin/bash
# 「端末へ飛ぶ」が窓とタブの並び順によらず成功することを、実機で繰り返し確かめる
# (ms-173 e-7219)。
#
# なぜ手で走らせる形なのか: 失敗は AppleScript が実際の端末アプリを操作したときに
# しか起きない (窓の前後関係が変わることが引き金)。CI には端末アプリが無いので、
# 構造の禁止は Go のテスト (TestJumpScriptsDoNotMutateWhileScanning) が常時見張り、
# 実機の挙動はこのスクリプトで人が確かめる、という二段にしている。
#
# 使い方:
#   bash scripts/verify-jump-terminal-reorder.sh iterm2     # iTerm2 を検証
#   bash scripts/verify-jump-terminal-reorder.sh terminal   # Terminal.app を検証
#   bash scripts/verify-jump-terminal-reorder.sh iterm2 --with-old   # 旧形式も並べて測る
#
# 事前条件 (これを満たさないと「落ちない条件」だけ測って緑になる):
#   - 対象アプリの窓を 3 つ以上開く
#   - そのうち 1 つは**タブを 3 枚以上**にする
#   - 失敗は「複数タブの窓の、最後でないタブに一致し、かつその窓が前面でない」
#     ときに出る。前面の窓に一致する場合は並びが変わらないので落ちない。
#     => このスクリプトは前面の窓を全通り変えながら全 tty を試すので、条件を必ず踏む。
#
# 終了コードは**閉じた enum** (独立 AX レビュー指摘、e-7219):
#
#   0 = 証拠になる成功   —— 事前条件を満たした上で全試行が通った
#   1 = 実機で失敗を再現 —— 現行スクリプトが 1 件以上落ちた
#   2 = 使い方の誤り     —— 引数が不正 / 対象アプリが起動していない / 生成器が転んだ
#   3 = 前提不足で未検証 —— 窓やタブが足りず失敗条件を踏めない (= 緑でも証拠にならない)
#
# **3 を 0 と区別するのがこの enum の肝。** 以前は事前条件が未達でも警告を stderr に
# 出すだけで exit 0 を返していた。`echo $?` だけを見る呼び出し側 (とくに AI) は、
# 「直った証拠にならない実行」を「検証通過」と読む。このスクリプト自身が名指しで
# 警告している落とし穴に、自分の出口が落ちていた。
set -uo pipefail

usage() {
  echo "使い方: $0 [iterm2|terminal] [--with-old]" >&2
  echo "  終了コード: 0=証拠になる成功 / 1=実機で失敗を再現 / 2=使い方の誤り / 3=前提不足で未検証" >&2
}

APP_KEY="${1:-iterm2}"
# 第 2 引数は許可集合で閉じる (独立 AX レビュー指摘、e-7219)。以前はリテラル
# `--with-old` との完全一致だけを見ていたため、タイプミス (`--with-olds` 等) が
# 黙って無視され、旧形式を一度も走らせないまま「比較できた」と誤認できた。
WITH_OLD=0
case "${2:-}" in
  "")          WITH_OLD=0 ;;
  --with-old)  WITH_OLD=1 ;;
  *) echo "不明な引数: ${2}" >&2; usage; exit 2 ;;
esac
if [[ $# -gt 2 ]]; then
  echo "引数が多すぎます (${#} 個)" >&2; usage; exit 2
fi

# 対応端末を足すときは、ここと viewer/jump.go の terminalDrivers の**両方**に足す。
# AppleScript のオブジェクトモデルが端末ごとに違い (下の ENUM 参照)、列挙の書き方を
# テーブルから導出できないため、ここは自動化されていない (jump.go 側にも相互参照あり)。
case "$APP_KEY" in
  iterm2)   APP="iTerm2";   HARNESS="iterm2" ;;
  terminal) APP="Terminal"; HARNESS="apple-terminal" ;;
  *) echo "不明な端末: $APP_KEY" >&2; usage; exit 2 ;;
esac

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

osascript -e "tell application \"System Events\" to return (name of processes) contains \"$APP\"" \
  | grep -q true || { echo "$APP が起動していません。開いてから再実行してください。" >&2; exit 2; }

# --- 窓 id と tty を列挙 (読み取りのみ) -------------------------------------
# 出力は 3 列: <物理ウィンドウの鍵> <window id> <tty>
#
# **2 つの端末でモデルが違う** (e-7219 の実測で判明):
#   - iTerm2:       window -> tabs -> sessions。タブは窓の中に入れ子になる。
#                   物理ウィンドウの鍵 = window id。
#   - Terminal.app: **タブが 1 つずつ別の `window` オブジェクトとして見える**
#                   (どれも tabs=1)。だから window id で数えると、3 タブの窓でも
#                   「1 窓 1 タブ」に見えてしまう。物理ウィンドウの鍵には
#                   `bounds` (画面上の位置と大きさ) を使う —— 同じ物理ウィンドウの
#                   タブは bounds が一致する。
# 鍵を取り違えると、タブが足りているのに「足りない」と警告してしまい、
# 検証そのものが使えなくなる (実際にそうなっていた)。
if [[ "$APP_KEY" == "iterm2" ]]; then
  ENUM='tell application "iTerm2"
set o to ""
repeat with w in windows
repeat with t in tabs of w
repeat with s in sessions of t
set o to o & (id of w as text) & " " & (id of w as text) & " " & (tty of s) & linefeed
end repeat
end repeat
end repeat
return o
end tell'
else
  ENUM='tell application "Terminal"
set o to ""
repeat with w in windows
repeat with t in tabs of w
set o to o & "b" & (bounds of w as text) & " " & (id of w as text) & " " & (tty of t) & linefeed
end repeat
end repeat
return o
end tell'
fi
osascript -e "$ENUM" | sed 's#/dev/##' | tr -d ',' | awk 'NF==3' > "$WORK/pairs.txt"

# 列挙が 1 件も取れないと、以降の配列参照が set -u で落ちて exit 1 (= 実機で失敗を
# 再現した) として出てしまう。試行を 1 度も走らせていないのに「失敗を再現」と名乗るのは
# 終了コードの嘘なので、ここで 2 (= 使い方 / 環境の誤り) として明示的に止める。
# 起こる経路: アプリは起動しているが列挙が空 (= ENUM がこの版に合っていない / 窓が無い)。
if [[ ! -s "$WORK/pairs.txt" ]]; then
  echo "$APP の窓を 1 つも列挙できませんでした。窓が開いているか、" >&2
  echo "この版で ENUM (osascript の列挙) が通るかを確認してください。" >&2
  echo "  (試行を 1 度も走らせていないので、失敗の再現とは区別して 2 を返します)" >&2
  exit 2
fi

WINS=($(awk '{print $2}' "$WORK/pairs.txt" | sort -u))
TTYS=($(awk '{print $3}' "$WORK/pairs.txt" | sort -u))
PHYS=$(awk '{print $1}' "$WORK/pairs.txt" | sort -u | wc -l | tr -d ' ')
echo "$APP: 物理ウィンドウ $PHYS 個 / 端末オブジェクト ${#WINS[@]} 個 / tty ${#TTYS[@]} 本"
MULTI=$(awk '{c[$1]++} END{m=0; for (k in c) if (c[k]>m) m=c[k]; print m}' "$WORK/pairs.txt")
echo "  1 物理ウィンドウあたり最大タブ数: $MULTI"
INCONCLUSIVE=0
if (( MULTI < 3 )); then
  echo "  ⚠ タブ 3 枚以上の窓がありません。この状態では失敗条件を踏まないので、" >&2
  echo "    緑になっても「直った」証拠になりません。タブを増やして再実行してください。" >&2
  INCONCLUSIVE=1
fi
if (( PHYS < 3 )); then
  echo "  ⚠ 物理ウィンドウが 3 つ未満です。並び替えの余地が小さいので同じ理由で証拠になりません。" >&2
  INCONCLUSIVE=1
fi

# --- 本番と同じ生成器から AppleScript を取る (写さない) ----------------------
TTY_CSV="$(IFS=,; echo "${TTYS[*]}")"
if ! (cd "$ROOT/viewer" && \
      BEACON_JUMP_SCRIPT_OUT="$WORK" \
      BEACON_JUMP_SCRIPT_TTYS="$TTY_CSV" \
      BEACON_JUMP_SCRIPT_HARNESS="$HARNESS" \
      BEACON_JUMP_SCRIPT_LEGACY=1 \
      go test -count=1 -run TestDumpJumpScriptsForManualVerification . >/dev/null); then
  echo "AppleScript を生成できませんでした (go test 失敗)" >&2
  exit 2
fi

# --- 前面の窓を変えながら全 tty を試す ---------------------------------------
preface() {
  if [[ "$APP_KEY" == "iterm2" ]]; then
    osascript -e "tell application \"iTerm2\" to select (first window whose id is $1)" >/dev/null 2>&1
  else
    osascript -e "tell application \"Terminal\" to set frontmost of (first window whose id is $1) to true" >/dev/null 2>&1
  fi
}
ORIG="${WINS[0]}"

PASS=0; FAIL=0
for front in "${WINS[@]}"; do
  for tty in "${TTYS[@]}"; do
    preface "$front"
    if out=$(osascript "$WORK/jump_$tty.applescript" 2>&1); then
      PASS=$((PASS+1))
    else
      FAIL=$((FAIL+1))
      echo "  FAIL front=$front tty=$tty: $out"
    fi
  done
done
echo "新 (現行) : 成功 $PASS / 失敗 $FAIL  (試行 $((PASS+FAIL)) = 端末オブジェクト ${#WINS[@]} x tty ${#TTYS[@]})"

if (( WITH_OLD )); then
  # 旧形式 (走査しながら選択する形) を同条件で測り、条件が失敗を踏むことを示す。
  # ここが 0 失敗なら、測った条件が緩すぎる = 新形式の緑も証拠にならない。
  # **スクリプトは旧形式を手書きしない** —— 正典は jump_test.go の
  # legacyMutatingJumpScript 1 箇所で、上の生成器呼び出しが書き出したものを読む
  # (独立保守性レビュー指摘: 2 箇所に手書きすると、片方だけ直したとき
  #  「単体テストが検出する旧形式」と「実機で測る旧形式」が別物を指す)。
  OP=0; OF=0
  for front in "${WINS[@]}"; do
    for tty in "${TTYS[@]}"; do
      preface "$front"
      if [[ ! -f "$WORK/legacy_$tty.applescript" ]]; then
        echo "旧形式の生成物がありません ($WORK/legacy_$tty.applescript)" >&2
        exit 2
      fi
      if osascript "$WORK/legacy_$tty.applescript" >/dev/null 2>&1; then
        OP=$((OP+1))
      else
        OF=$((OF+1))
      fi
    done
  done
  echo "旧 (修正前): 成功 $OP / 失敗 $OF"
  if (( OF == 0 )); then
    echo "  ⚠ 旧形式が 1 件も落ちていません。測った条件が失敗を踏んでいないので、" >&2
    echo "    新形式の緑は「直った」証拠になりません (事前条件を見直してください)。" >&2
    INCONCLUSIVE=1
  fi
fi

preface "$ORIG"

# 出口は閉じた enum (冒頭の契約)。失敗の再現 (1) が最優先、次に前提不足 (3)。
# 「全試行通ったが前提不足」を 0 で返さないのがこの関門の目的。
if (( FAIL > 0 )); then
  exit 1
fi
if (( INCONCLUSIVE )); then
  echo "→ 前提不足のため未検証 (exit 3)。全試行は通ったが、失敗条件を踏めていないので証拠になりません。" >&2
  exit 3
fi
echo "→ 証拠になる成功 (exit 0)。"
