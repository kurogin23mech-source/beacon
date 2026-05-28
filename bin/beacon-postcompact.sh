#!/bin/bash
# beacon-postcompact.sh
#
# PostCompact hook: コンパクション直後の AI に「Tier 2 軽量オリエンテーション」
# を inject する。フル /beacon-session-start は強制せず、AI が summary 内の
# 具体ID/数値/決定を鵜呑みにせず正源を再 fetch するメンタルモデルで続行
# できるようにする (e-565)。
#
# Hook type: PostCompact
# Payload (stdin JSON):
#   { "session_id": "...", "transcript_path": "...", ... }
#
# Output: systemMessage を含む JSON を stdout に出力する。
# Claude Code はこれを context として AI に渡す。
#
# 自分が動作する条件:
#   1. cwd (または親) に .beacon/project.json が存在する
#   2. beacon CLI が呼べる
# どちらも欠ければ静かに exit 0 する。

set -euo pipefail

log() { echo "[postcompact] $*" >&2; }

# ─── beacon プロジェクト検出 ───────────────────────────────────────────────────
find_beacon_root() {
  local dir="$1"
  while [ "$dir" != "/" ] && [ -n "$dir" ]; do
    if [ -f "$dir/.beacon/project.json" ]; then
      echo "$dir"
      return 0
    fi
    dir="$(dirname "$dir")"
  done
  return 1
}

# hook が呼ばれる cwd は claude code セッションの作業 dir。それを起点に上に遡る。
# `|| true` を付けないと set -e のもとで non-zero exit がスクリプト全体を落とす。
BEACON_ROOT=$(find_beacon_root "$(pwd)" || true)
if [ -z "$BEACON_ROOT" ]; then
  exit 0
fi

# beacon CLI が無ければ何もしない (環境依存で fail-safe にする)
if ! command -v beacon >/dev/null 2>&1; then
  log "beacon CLI not found — skipping"
  exit 0
fi

cd "$BEACON_ROOT"

# ─── 軽量コンテキスト取得 ──────────────────────────────────────────────────────
# 重い session-start 全体は走らせない。necessary minimum だけ取る:
#  - active MS の id / title / progress
#  - 直近 session note の先頭行 (= 見出し)
# これらは「具体 ID と直近の判断」のアンカーとして使える。
# 出力が壊れていても hook を fail させない (best-effort)。

STATUS_JSON=$(beacon status --json 2>/dev/null || echo "")
NOTES_JSON=$(beacon note list --json 2>/dev/null || echo "[]")

CONTEXT_TEXT=$(python3 - "$STATUS_JSON" "$NOTES_JSON" <<'PYEOF'
import json, sys

status_raw = sys.argv[1]
notes_raw = sys.argv[2]

lines = []

try:
    status = json.loads(status_raw) if status_raw else {}
except Exception:
    status = {}

name = status.get("name", "")
if name:
    lines.append(f"プロジェクト: {name}")

active_ms = []
for ms in status.get("milestones", []) or []:
    if ms.get("status") == "in_progress":
        active_ms.append(ms)

if active_ms:
    lines.append("Active MS:")
    for ms in active_ms:
        lines.append(
            f"  - {ms.get('id','?')} \"{ms.get('title','')}\""
            f" ({ms.get('progress',0)}% / {ms.get('done_tasks',0)}/{ms.get('total_tasks',0)}タスク完了)"
        )
else:
    lines.append("Active MS: なし")

active_ops = []
for op in status.get("operations", []) or []:
    if op.get("status") == "open":
        active_ops.append(op)

if active_ops:
    lines.append("Active Operation:")
    for op in active_ops:
        lines.append(f"  - {op.get('id','?')} \"{op.get('title','')}\"")

# Notes (heading line only, up to 3 most recent)
try:
    notes = json.loads(notes_raw) if notes_raw else []
except Exception:
    notes = []

# Pick up to 3 most recent notes' first heading lines.
recent_headings = []
for note in (notes or [])[-3:]:
    text = (note.get("text") or "").strip()
    first_line = text.split("\n", 1)[0].strip().lstrip("#").strip()
    if first_line:
        recent_headings.append(first_line)

if recent_headings:
    lines.append("直近 note 見出し:")
    for h in recent_headings:
        lines.append(f"  - {h}")

print("\n".join(lines))
PYEOF
)

# ─── systemMessage の組み立て ──────────────────────────────────────────────────
# 「具体ID/数値/決定は再 fetch せよ」のソフトな指示を埋め込む。フル session-start
# 強制ではなく、AI 判断に任せる (SPEC 受入条件 4 通り)。

if [ -z "$CONTEXT_TEXT" ]; then
  CONTEXT_TEXT="（beacon status の取得に失敗しました。/beacon-session-start で復元してください）"
fi

read -r -d '' MESSAGE <<EOM || true
BEACON [post-compaction orientation]
コンパクションが発生しました。summary は内容を保持していますが、具体ID・数値・直近の決定は抽象化されている可能性があります。下記は正源 (.beacon/project.json) から再取得した現在地です:

$CONTEXT_TEXT

判断指針:
- summary 内の具体ID (ms-XX / e-XX / op-XX)・数値・直近 1 セッションの決定は鵜呑みにせず、必要なら beacon コマンドで再 fetch してください。
- 細部 (タスク一覧・SPEC・直近コミット) が必要なら /beacon-session-start を実行してください。
- メモやサマリーで足りる範囲なら、そのまま作業を続けて構いません。
EOM

# ─── Claude への通知出力 ───────────────────────────────────────────────────────
python3 - "$MESSAGE" <<'PYEOF'
import json, sys
output = {"systemMessage": sys.argv[1]}
print(json.dumps(output, ensure_ascii=False))
PYEOF
