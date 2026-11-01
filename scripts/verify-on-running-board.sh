#!/usr/bin/env bash
# 盤の修正を、**動いている盤そのものに問い合わせて**確かめる (ms-170 e-7252)。
#
# **なぜ要るのか**: 2026-10-30 に「運用室から端末へ飛べない」不具合を直し、
# ソースから起こしたスクリプトを直接 48 回走らせて全て成功したことを根拠に閉じた。
# ところが 11-01 に同じ症状が観測された。動いていた実行ファイルが 10/28 の古いもの
# で、修正が入っていなかった。**機構がコードに在ることと、効いていることは別**。
# 生成物を単体で動かした結果は、動いている盤が直った証拠にならない。
#
# **なぜ前後で測るのか**: 直した後だけ成功しても、それが修正の効果とは言えない。
# 元の不具合は窓とタブの並び順に依存して 3 回に 1 回ほど失敗する形だったので、
# 片方だけ見ていると「たまたま成功した」と区別がつかない。前の実行ファイルと
# 新しい実行ファイルを **別の口で同時に立て、同じ的に同じ要求を投げて差を見る**。
#
# **なぜ 1 的 1 回では足りないのか**: 並び順に依存する不具合では、先に投げた側が
# 並び順を変えて、後から投げた側を成功させてしまう。前後に同じ古い実行ファイルを
# 渡して試したとき「直った」と報告した (= 偽陽性)。各的を数回測り、毎回どちらを
# 先に投げるかを入れ替えることで、順序の恩恵が片側に偏らないようにしている。
#
# 使い方:
#   scripts/verify-on-running-board.sh <古い実行ファイル> [新しい実行ファイル]
#
#   例 (取り込み前の実行ファイルを控えておいて比べる):
#     git stash && ./viewer/build.sh darwin && cp viewer/dist/beacon-view-darwin-arm64 /tmp/before
#     git stash pop && ./viewer/build.sh darwin
#     scripts/verify-on-running-board.sh /tmp/before
#
# 新しい側を省略すると viewer/dist/ の現物を使う。
#
# 出すもの: 的ごとの前後の結果と、最後に 1 行の判定。
#   - 前で失敗が在り、後で全て成功 → 直ったと言える
#   - 前後とも全て成功 → この的では差が出ない (再現条件を狭めるか的を増やす)
#   - 後に失敗が在る → 直っていない
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BEFORE="${1:-}"
AFTER="${2:-$ROOT/viewer/dist/beacon-view-darwin-arm64}"

if [ -z "$BEFORE" ]; then
    sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
fi
for bin in "$BEFORE" "$AFTER"; do
    if [ ! -x "$bin" ]; then
        echo "Error: 実行できません: $bin" >&2
        exit 2
    fi
done

PORT_BEFORE="${PORT_BEFORE:-7891}"
PORT_AFTER="${PORT_AFTER:-7892}"
TMP="$(mktemp -d)"
PIDS=()

cleanup() {
    for p in "${PIDS[@]:-}"; do
        [ -n "$p" ] && kill -TERM "$p" 2>/dev/null
    done
}
trap cleanup EXIT

# 盤を 1 本立てて、応えるようになるまで待つ。
#
# 再利用 (e-7147) と居残りの自動終了は **意図して切る**: ここでは「この実行
# ファイルの盤」を名指しで立てたいので、既に在る盤に繋がれては測定にならない。
start_board() {
    local bin="$1" port="$2" log="$3"
    "$bin" --path "$ROOT" --port "$port" --host 127.0.0.1 --no-open \
        --no-reuse --idle-timeout 0 >"$log" 2>&1 &
    PIDS+=("$!")
    for _ in $(seq 1 40); do
        if curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$port/api/state"; then
            return 0
        fi
        sleep 0.25
    done
    echo "Error: $bin が $port で応えません (log: $log)" >&2
    sed -n '1,5p' "$log" >&2
    return 1
}

echo "前の実行ファイル: $BEFORE"
echo "後の実行ファイル: $AFTER"
echo
start_board "$BEFORE" "$PORT_BEFORE" "$TMP/before.log" || exit 1
start_board "$AFTER" "$PORT_AFTER" "$TMP/after.log" || exit 1

# 的を集める。盤自身が知っているセッション一覧から取るので、環境によって的の数は
# 変わる (固定の pid を書き込まない = 別の機械でも走る)。
targets() {
    curl -s --max-time 5 "http://127.0.0.1:$PORT_AFTER/api/sessions" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
rows = d if isinstance(d, list) else (d.get("sessions") or [])
for r in rows:
    pid, harness = r.get("pid"), r.get("harness") or ""
    if pid and harness:
        print(pid, harness)
'
}

# `mapfile` は使わない: macOS が同梱する bash は 3.2 で、この組み込みを持たない
# (実測でここだけ落ちた)。読み込みの繰り返しで組む。
TARGETS=()
while IFS= read -r _line; do
    [ -n "$_line" ] && TARGETS+=("$_line")
done < <(targets)
if [ "${#TARGETS[@]:-0}" -eq 0 ]; then
    echo "的が 0 件でした。端末のセッションが見えていないと前後の差は測れません。" >&2
    echo "  (盤を立てた端末で走らせてください)" >&2
    exit 2
fi

# 同じ的に同じ要求を投げる。
probe() {
    local port="$1" pid="$2" harness="$3"
    curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
        -X POST -H 'Content-Type: application/json' \
        -d "{\"pid\":$pid,\"harness\":\"$harness\"}" \
        "http://127.0.0.1:$port/api/jump"
}

# **的ごとに交互に投げてはいけない** (実測で 2 度失敗した):
#
# 問い合わせ自体が測定対象 (窓とタブの並び) を変える。的ごとに前→後と投げると
# 先に投げた側が不利になり、前後に同じ古い実行ファイルを渡しても「直った」と
# 出た (偽陽性)。毎回順序を入れ替えても、今度は本物の差まで消えた (偽陰性) ——
# 交互に投げる限り、並びが毎回崩れて独立した反復測定にならない。
#
# **周 (パス) 単位で測る**。片側で全的を 1 周 → もう片側で全的を 1 周、を 1 組と
# して繰り返し、**組ごとにどちらの周を先にするかを入れ替える**。判定の根拠は
# 「**後から周を回した側**で失敗が出たか」。先に回った側が並びを整えた後でも
# 失敗するなら、それは順序では説明できない = 中身の違い。
PAIRS="${PAIRS:-2}"

# 1 周 = 全的に 1 回ずつ投げ、失敗数を返す。
sweep() {
    local port="$1" fails=0 row pid harness rc
    for row in "${TARGETS[@]:-}"; do
        [ -n "$row" ] || continue
        pid="${row%% *}"
        harness="${row#* }"
        rc="$(probe "$port" "$pid" "$harness")"
        [ "$rc" = "200" ] || fails=$((fails + 1))
    done
    echo "$fails"
}

total_targets="${#TARGETS[@]}"
before_second_fails=0   # 後から周を回ったときの前側の失敗 (判定の根拠)
after_second_fails=0
sum_before=0
sum_after=0
printf '%-6s %-10s %-14s %-14s\n' "組" "先に回る" "前(失敗/的)" "後(失敗/的)"
k=1
while [ "$k" -le "$PAIRS" ]; do
    if [ $((k % 2)) -eq 1 ]; then
        fb="$(sweep "$PORT_BEFORE")"
        fa="$(sweep "$PORT_AFTER")"
        after_second_fails=$((after_second_fails + fa))
        first="前"
    else
        fa="$(sweep "$PORT_AFTER")"
        fb="$(sweep "$PORT_BEFORE")"
        before_second_fails=$((before_second_fails + fb))
        first="後"
    fi
    sum_before=$((sum_before + fb))
    sum_after=$((sum_after + fa))
    printf '%-6s %-10s %-14s %-14s\n' "$k" "$first" \
        "$fb/$total_targets" "$fa/$total_targets"
    k=$((k + 1))
done

echo
echo "的 $total_targets 件 × $PAIRS 組 (= 片側 $((PAIRS * total_targets)) 試行)"
echo "前: 合計 $sum_before 件失敗 (後から回ったとき $before_second_fails 件)"
echo "後: 合計 $sum_after 件失敗 (後から回ったとき $after_second_fails 件)"
echo
if [ "$sum_after" -gt 0 ]; then
    echo "判定: 直っていません (後の側で合計 $sum_after 件失敗)"
    exit 1
fi
if [ "$before_second_fails" -gt 0 ]; then
    echo "判定: 直ったと言えます"
    echo "      前の側は、後の側が先に周を回して並びを整えた後でも"
    echo "      $before_second_fails 件失敗しました。順序では説明できないので、"
    echo "      中身の違いによるものです。後の側は合計 0 件。"
    exit 0
fi
if [ "$sum_before" -eq 0 ]; then
    echo "判定: 証拠になりません。前の側も 1 件も失敗していないので、この的では"
    echo "      差が出ません (再現条件を狭めるか、的を増やしてください)"
    exit 3
fi
echo "判定: 証拠として弱いです。前の側の失敗 $sum_before 件は、すべて先に周を"
echo "      回ったときのものでした。問い合わせは窓の並びを変えるので、先に回った"
echo "      側は不利になります —— この差は順序で説明できてしまいます。"
echo "      PAIRS を増やして測り直してください (例: PAIRS=6 $0 ...)"
exit 3
