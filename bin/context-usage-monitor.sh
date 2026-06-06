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
#
# Context limit detection (e-561):
#   1. BEACON_CONTEXT_LIMIT env var (explicit override, integer tokens)
#   2. transcript の最新 assistant message.model から自動判定
#      - Opus / Sonnet 4.x の "[1m]" suffix が付くもの → 1,000,000
#      - それ以外の Claude モデル → 200,000
#   3. fallback: 200,000

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

# ─── transcript から最新 assistant メッセージの usage + model を取得 ───────────
# JSONL の各行を逆順で走査して最初に見つかった usage を使う。同時に model 名も
# 拾って context_limit 判定に使う (e-561)。
USAGE_AND_MODEL=$(python3 - "$TRANSCRIPT_PATH" <<'PYEOF'
import json, sys

path = sys.argv[1]
try:
    with open(path) as f:
        lines = f.readlines()
except Exception:
    print("{}", end="")
    sys.exit(0)

for line in reversed(lines):
    try:
        d = json.loads(line)
        msg = d.get('message', {})
        if msg.get('role') == 'assistant' and 'usage' in msg:
            out = {"usage": msg['usage'], "model": msg.get('model', '')}
            print(json.dumps(out), end="")
            sys.exit(0)
    except Exception:
        continue

print("{}", end="")
PYEOF
)

if [ "$USAGE_AND_MODEL" = "{}" ] || [ -z "$USAGE_AND_MODEL" ]; then
  log "no assistant usage data found in transcript — skipping"
  exit 0
fi

USAGE_JSON=$(echo "$USAGE_AND_MODEL" | jq -c '.usage // {}')
MODEL_NAME=$(echo "$USAGE_AND_MODEL" | jq -r '.model // ""')

# ─── Context limit の判定 (e-561) ──────────────────────────────────────────────
# 優先順位: BEACON_CONTEXT_LIMIT env var > model 名から推定 > 200K fallback.
CONTEXT_LIMIT="${BEACON_CONTEXT_LIMIT:-}"
if [ -z "$CONTEXT_LIMIT" ]; then
  # Claude 4.7 / 4.x の 1M ベータ系は model id に "[1m]" を含む
  # 例: "claude-opus-4-7[1m]", "claude-sonnet-4-6[1m]"
  if echo "$MODEL_NAME" | grep -qE '\[1m\]|-1m$|1m-'; then
    CONTEXT_LIMIT=1000000
  else
    CONTEXT_LIMIT=200000
  fi
fi
log "context_limit=$CONTEXT_LIMIT (model=${MODEL_NAME:-unknown})"

# ─── コンテキスト使用量の計算 ──────────────────────────────────────────────────
CURRENT_CONTEXT=$(echo "$USAGE_JSON" | jq -r '
  ((.input_tokens // 0) + (.cache_read_input_tokens // 0) + (.cache_creation_input_tokens // 0))
')

if [ -z "$CURRENT_CONTEXT" ] || [ "$CURRENT_CONTEXT" = "null" ]; then
  log "failed to parse usage tokens — skipping"
  exit 0
fi

# 使用率 % (整数) — denominator は context_limit に動的化
PERCENT=$(python3 -c "print(int($CURRENT_CONTEXT / $CONTEXT_LIMIT * 100))")

# コンパクション後の継続セッションで 100% 超える場合はスキップ
if [ "$PERCENT" -gt 100 ]; then
  log "percent=$PERCENT > 100 (post-compaction continuation) — skipping"
  exit 0
fi

log "session=$SESSION_ID context=$CURRENT_CONTEXT/${CONTEXT_LIMIT} percent=${PERCENT}%"

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
REMAINING=$((CONTEXT_LIMIT - CURRENT_CONTEXT))
CONTEXT_APPROX="$CURRENT_CONTEXT"
# 表示用に CONTEXT_LIMIT を 3-digit カンマ区切りで整形 (Bash 内で完結)
CONTEXT_LIMIT_FMT=$(python3 -c "print(f'{int($CONTEXT_LIMIT):,}')")

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

# ─── 現在地の自動取得（beacon status --json）──────────────────────────────────
LOCATION_TEXT=""
STATUS_JSON=$(beacon status --json 2>/dev/null || echo "")
if [ -n "$STATUS_JSON" ]; then
  LOCATION_TEXT=$(python3 - "$STATUS_JSON" <<'PYEOF'
import json, sys
try:
    d = json.loads(sys.argv[1])
    lines = []
    for ms in d.get("milestones", []):
        if ms.get("status") == "in_progress":
            lines.append(f"- Active MS: {ms['id']} \"{ms['title']}\" ({ms.get('progress',0)}% / {ms.get('done_tasks',0)}/{ms.get('total_tasks',0)}タスク完了)")
    for op in d.get("operations", []):
        if op.get("status") == "open":
            lines.append(f"- Active Operation: {op['id']} \"{op['title']}\"")
    print("\n".join(lines))
except Exception:
    pass
PYEOF
)
fi

# ─── 自動ノート記録（auto-enriched template, ms-31 follow-up）────────────────
#
# 元の設計では「Claude が後で /beacon-note で埋める」前提だったが、85994cf で
# decision:block → systemMessage に降格したことで、Claude が応じる保証が消えた。
# 結果: 残された note が template-only の空骨組みになる問題。
#
# Fix: script 側で取得可能な情報 (recent commits / pending tasks) を自動で
# 埋める。Claude が追記しないケースでも note 自体は意味のある状態スナップショット
# になる。Claude 追記用 section は「任意」と明示して compaction cascade の元と
# なる "必ず埋めて" 圧を下げる。
NOTE_DATE=$(date +"%Y-%m-%d %H:%M")

# 直近コミット (過去 6 時間 — 1 セッションがその辺りに収まる目安)
# set -euo pipefail が効いているので、git が失敗するケース (cwd が git repo
# でない、git 自体未インストール等) でもパイプ全体を fail させないように
# || true を最後に置く。空でもフォールバックメッセージを出す。
RECENT_COMMITS_TEXT="$(
  { git log --since="6 hours ago" --pretty=format:"- %h: %s" 2>/dev/null || true; } | head -10
)"
if [ -z "$RECENT_COMMITS_TEXT" ]; then
  RECENT_COMMITS_TEXT="（直近6時間にコミットなし）"
fi

# Active MS の未消化タスク (priority 順、最大 8 件)
PENDING_TASKS_TEXT=$(
  STATUS_JSON_FOR_TASKS=$(beacon status --json 2>/dev/null || echo "")
  if [ -n "$STATUS_JSON_FOR_TASKS" ]; then
    python3 - "$STATUS_JSON_FOR_TASKS" <<'PYEOF' 2>/dev/null
import json, sys
try:
    d = json.loads(sys.argv[1])
    active = next((ms for ms in d.get("milestones", []) if ms.get("status") == "in_progress"), None)
    if not active:
        print("（active MS なし）")
        sys.exit(0)
    import subprocess
    out = subprocess.run(["beacon", "task", "list", "--ms", active["id"], "--json"],
                          capture_output=True, text=True, timeout=5)
    if out.returncode != 0:
        print("（タスク取得失敗）")
        sys.exit(0)
    tasks = json.loads(out.stdout)
    pending = []
    def walk(entries):
        for e in entries or []:
            if e.get("type") == "task" and e.get("status") != "done":
                p = (e.get("meta") or {}).get("priority", "-")
                pending.append((p, e.get("id",""), e.get("description","")[:120]))
            walk(e.get("entries", []))
    walk(tasks.get("entries", []))
    order = {"highest":0,"high":1,"middle":2,"low":3,"lowest":4,"-":5}
    pending.sort(key=lambda t: order.get(t[0], 9))
    if not pending:
        print("（未消化タスクなし）")
    else:
        for p, eid, desc in pending[:8]:
            print(f"- [{p:7}] {eid}: {desc}")
except Exception as e:
    print(f"（取得エラー: {type(e).__name__}）")
PYEOF
  else
    echo "（beacon status 取得失敗）"
  fi
)

NOTE_BODY="## コンテキストサマリー（自動記録 ${PERCENT}% / ${NOTE_DATE}）

### 現在地（自動取得）
${LOCATION_TEXT:-（取得失敗）}

### 直近のコミット（過去 6 時間 / 自動取得）
${RECENT_COMMITS_TEXT}

### Active MS の未消化タスク（優先度順 / 自動取得）
${PENDING_TASKS_TEXT}

### このセッションで決めたこと（任意 — Claude が追記）
（残りはコンテキスト次第で Claude が追記。空欄でも note 自体は状態スナップショットとして有効。）

### 次のアクション（任意 — Claude が追記）
"

beacon note "$NOTE_BODY" 2>/dev/null || true

# ─── Claude への通知出力（追記は任意トーン）────────────────────────────
# ms-31 follow-up: 元は「必ず /beacon-note で埋めて」という強制トーンだったが、
# script 側で recent commits + pending tasks まで埋めるようになったので
# Claude 追記は任意。「会話特有の判断軌跡があれば beacon note で追記してください」
# 程度の suggestion にとどめ、compaction cascade のリスクを下げる。
TEMPLATE_INSTRUCTION="BEACON [コンテキスト ${PERCENT}% / ${CURRENT_CONTEXT}/${CONTEXT_LIMIT_FMT} tokens]
閾値 ${TRIGGERED_THRESHOLD}% 到達。session note を自動記録しました
(現在地 / 直近コミット / 未消化タスク を script が自動取得済)。

beacon note list で内容を確認できます。会話特有の判断軌跡（例: なぜこの設計を選んだか、
次セッションで知っておくべき非自明なコンテキスト）があれば、
\`beacon note \"text\"\` で追記してください — 任意です。"

python3 - "$TEMPLATE_INSTRUCTION" <<'PYEOF'
import json, sys
# Stop hook: additionalContext は次の Claude turn 開始時に自動注入される。
# systemMessage は terminal にしか出ず Claude には届かないため、
# /beacon-note を「実行してほしい」依頼が Claude に届かず空テンプレートの
# まま放置される問題があった (ms-31)。hookSpecificOutput.additionalContext
# で渡すと、ユーザーが次に何か発言した瞬間 Claude のコンテキストに乗る。
output = {
    "hookSpecificOutput": {
        "hookEventName": "Stop",
        "additionalContext": sys.argv[1],
    }
}
print(json.dumps(output, ensure_ascii=False))
PYEOF
