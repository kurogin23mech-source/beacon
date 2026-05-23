#!/bin/bash
# context-usage-monitor.sh
# Stop hook: コンテキスト使用率を監視し、閾値到達時に Claude へ通知する
#
# Hook type: Stop
# Payload (stdin JSON):
#   { "session_id": "...", "transcript_path": "/path/to/session.jsonl", ... }
#
# 通知は hookSpecificOutput.additionalContext に含める
# 閾値: 20 / 40 / 60 / 80 (%)、各閾値に初回到達時のみ通知
# 状態ファイル: .claude/context-usage-state.json

set -euo pipefail

# ─── ユーティリティ ────────────────────────────────────────────────────────────
log() { echo "[context-monitor] $*" >&2; }

# ─── Beacon プロジェクト検証 ────────────────────────────────────────────────────
if [ ! -f .beacon/project.json ]; then
  exit 0
fi

# ─── stdin から hook payload を読む ────────────────────────────────────────────
INPUT=$(cat /dev/stdin)

SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)
TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // empty' 2>/dev/null)

if [ -z "$SESSION_ID" ] || [ -z "$TRANSCRIPT_PATH" ]; then
  log "session_id or transcript_path not found in payload — skipping"
  exit 0
fi

if [ ! -f "$TRANSCRIPT_PATH" ]; then
  log "transcript file not found: $TRANSCRIPT_PATH — skipping"
  exit 0
fi

# ─── transcript から最新 assistant メッセージの usage を取得 ────────────────────
# JSONL の各行を逆順で走査して最初に見つかった usage を使う
USAGE_JSON=$(python3 - "$TRANSCRIPT_PATH" <<'PYEOF'
import json, sys

path = sys.argv[1]
try:
    with open(path) as f:
        lines = f.readlines()
except Exception as e:
    print("{}", end="")
    sys.exit(0)

for line in reversed(lines):
    try:
        d = json.loads(line)
        msg = d.get('message', {})
        if msg.get('role') == 'assistant' and 'usage' in msg:
            print(json.dumps(msg['usage']), end="")
            sys.exit(0)
    except Exception:
        continue

print("{}", end="")
PYEOF
)

if [ "$USAGE_JSON" = "{}" ] || [ -z "$USAGE_JSON" ]; then
  log "no assistant usage data found in transcript — skipping"
  exit 0
fi

# ─── コンテキスト使用量の計算 ──────────────────────────────────────────────────
CURRENT_CONTEXT=$(echo "$USAGE_JSON" | jq -r '
  ((.input_tokens // 0) + (.cache_read_input_tokens // 0) + (.cache_creation_input_tokens // 0))
')

if [ -z "$CURRENT_CONTEXT" ] || [ "$CURRENT_CONTEXT" = "null" ]; then
  log "failed to parse usage tokens — skipping"
  exit 0
fi

# 使用率 % (整数)
PERCENT=$(python3 -c "print(int($CURRENT_CONTEXT / 200000 * 100))")

# コンパクション後の継続セッションで 100% 超える場合はスキップ
if [ "$PERCENT" -gt 100 ]; then
  log "percent=$PERCENT > 100 (post-compaction continuation) — skipping"
  exit 0
fi

log "session=$SESSION_ID context=$CURRENT_CONTEXT percent=${PERCENT}%"

# ─── 状態ファイルの読み込み・セッション管理 ────────────────────────────────────
STATE_FILE=".claude/context-usage-state.json"
mkdir -p .claude

# デフォルト状態
PREV_SESSION=""
NOTIFIED_THRESHOLDS="[]"

if [ -f "$STATE_FILE" ]; then
  PREV_SESSION=$(jq -r '.session_id // empty' "$STATE_FILE" 2>/dev/null || echo "")
  if [ "$PREV_SESSION" = "$SESSION_ID" ]; then
    NOTIFIED_THRESHOLDS=$(jq -r '.notified_thresholds // "[]"' "$STATE_FILE" 2>/dev/null || echo "[]")
  else
    log "session changed ($PREV_SESSION -> $SESSION_ID) — resetting state"
  fi
fi

# ─── 閾値チェック (20 / 40 / 60 / 80) ─────────────────────────────────────────
# 現在の % に達している全閾値のうち、最高の「未通知」閾値を探す
# ただし、それより低い全閾値は「自動的に通知済み」として記録する
# （例: 初回 67% に到達 → 20/40/60 を全て記録し、60% の通知を出す）

TRIGGERED_THRESHOLD=""
ALL_CROSSED=""

for T in 20 40 60 80; do
  if [ "$PERCENT" -ge "$T" ]; then
    ALL_CROSSED="$ALL_CROSSED $T"
    ALREADY_NOTIFIED=$(echo "$NOTIFIED_THRESHOLDS" | jq "contains([$T])")
    if [ "$ALREADY_NOTIFIED" = "false" ]; then
      # 未通知の閾値を発見 → 最高値を更新
      TRIGGERED_THRESHOLD="$T"
    fi
  fi
done

if [ -z "$TRIGGERED_THRESHOLD" ]; then
  # すでに全て通知済み or まだ閾値に達していない
  # 状態ファイルを最新セッションで更新（セッション変化があった場合のみ）
  if [ "$PREV_SESSION" != "$SESSION_ID" ]; then
    python3 - "$STATE_FILE" "$SESSION_ID" "$NOTIFIED_THRESHOLDS" <<'PYEOF'
import json, sys
state_file, session_id, thresholds_str = sys.argv[1], sys.argv[2], sys.argv[3]
thresholds = json.loads(thresholds_str)
with open(state_file, 'w') as f:
    json.dump({"session_id": session_id, "notified_thresholds": thresholds}, f)
PYEOF
  fi
  exit 0
fi

# ─── 通知メッセージの生成 ──────────────────────────────────────────────────────
REMAINING=$((200000 - CURRENT_CONTEXT))
CONTEXT_APPROX="$CURRENT_CONTEXT"

if [ "$TRIGGERED_THRESHOLD" -ge 80 ]; then
  ADVICE="BEACON [⚠️ コンテキスト ${PERCENT}%] ${CONTEXT_APPROX}/200,000 tokens — beacon noteに自動記録済み。必要なら /beacon-note で詳細サマリーを追加してください。"
else
  ADVICE="BEACON [コンテキスト ${PERCENT}%] ${CONTEXT_APPROX}/200,000 tokens — beacon noteに自動記録済み。"
fi

# ─── 状態ファイルの更新 ────────────────────────────────────────────────────────
# 現在 % 以下の全閾値を notified にマーク（スキップした閾値も含む）
CROSSED_JSON=$(python3 -c "
import json
crossed = [int(t) for t in '$ALL_CROSSED'.split() if t]
print(json.dumps(crossed))
")
NEW_THRESHOLDS=$(python3 - "$NOTIFIED_THRESHOLDS" "$CROSSED_JSON" <<'PYEOF'
import json, sys
existing = json.loads(sys.argv[1])
crossed = json.loads(sys.argv[2])
merged = sorted(set(existing + crossed))
print(json.dumps(merged))
PYEOF
)

python3 - "$STATE_FILE" "$SESSION_ID" "$NEW_THRESHOLDS" <<'PYEOF'
import json, sys
state_file, session_id, thresholds_str = sys.argv[1], sys.argv[2], sys.argv[3]
thresholds = json.loads(thresholds_str)
with open(state_file, 'w') as f:
    json.dump({"session_id": session_id, "notified_thresholds": thresholds}, f)
PYEOF

log "threshold ${TRIGGERED_THRESHOLD}% triggered — sending notification"

# ─── 自動ノート記録（Claude の応答を待たずに hook から直接記録）─────────────────
beacon note "⚠️ コンテキスト ${PERCENT}% (${CURRENT_CONTEXT} / 200,000 tokens) — 閾値 ${TRIGGERED_THRESHOLD}% 到達。次の区切りで /beacon-note でサマリーを記録してください。" 2>/dev/null || true

# ─── Claude への通知出力 ──────────────────────────────────────────────────────
# Stop hook の正しい出力フォーマット:
#   80%+: decision=block + reason → Claude が強制的にノート記録を実行
#   <80%: systemMessage → Claude が次のターンで参照するアドバイス（非ブロッキング）
python3 - "$ADVICE" <<'PYEOF'
import json, sys
advice = sys.argv[1]
# systemMessage のみ — ターン追加なし、スクリプトの beacon note が記録を保証
output = {
    "systemMessage": advice
}
print(json.dumps(output, ensure_ascii=False))
PYEOF
