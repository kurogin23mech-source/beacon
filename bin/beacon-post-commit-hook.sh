#!/bin/bash
# PostToolUse hook: detect git commit / push / deploy and prompt Claude to run beacon skills
# Called by Claude Code with JSON on stdin
#
# cwd-aware: respects tool_input.cwd so Claude Code launched in ~ can manage
# projects under ~/<name>/. Walks up from the resolved cwd to find the nearest
# .beacon/project.json.

INPUT=$(cat /dev/stdin)

# e-940: optional observability. When BEACON_HOOK_DEBUG=1, append a one-line
# decision record to ~/.beacon/hook-debug.log so the silent fail-safe outcomes
# (malformed / no-root / no-match / matched) can be diagnosed after the fact.
# Default (unset): no file, no stderr, exit 0 — existing contract unchanged.
_hook_debug() {
  [ "$BEACON_HOOK_DEBUG" = "1" ] || return 0
  local decision="$1" root="$2" cmd="$3"
  local home="${HOME:-$(cd ~ 2>/dev/null && pwd)}"
  local logdir="$home/.beacon"
  mkdir -p "$logdir" 2>/dev/null || return 0
  local ts
  ts=$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)
  # Flatten newlines so each record stays on one line (matches the Python
  # version's cmd.replace("\n", " )); heredoc commit bodies contain newlines.
  cmd=$(printf '%s' "$cmd" | tr '\r\n' '  ')
  printf '%s post-commit %s root=%s cmd=%.80s\n' \
    "$ts" "$decision" "${root:--}" "$cmd" >> "$logdir/hook-debug.log" 2>/dev/null
}

# Malformed stdin (not valid JSON) → silent exit, but record under debug.
if ! printf '%s' "$INPUT" | jq -e . >/dev/null 2>&1; then
  _hook_debug "malformed" "" "$INPUT"
  exit 0
fi

CMD=$(echo "$INPUT" | jq -r '.tool_input.command // ""')
# Bash tool's cwd parameter (Claude Code passes the cwd argument here when set)
TOOL_CWD=$(echo "$INPUT" | jq -r '.tool_input.cwd // empty')

# Strip content inside quoted strings AND heredoc bodies to avoid matching
# patterns that appear inside commit messages / docs being written via cat <<EOF
# (ms-43 e-613: heredoc body containing `git push` / `gcloud run deploy` was
# misfiring the push/deploy detection).
#
# Strategy:
#   1. Remove heredoc bodies: `<<[-]?'?MARKER'? ... MARKER` (any line containing
#      only the marker, optionally preceded by tabs for <<-). We approximate
#      this with a single awk pass so multi-line stdin works correctly.
#   2. Strip single- and double-quoted string contents (existing protection
#      against patterns inside --summary / --desc arguments).
CMD_BARE=$(printf '%s' "$CMD" | awk '
  BEGIN { in_heredoc = 0; marker = "" }
  {
    if (in_heredoc) {
      # End when the line (trimmed of leading tabs for <<-) equals marker
      line = $0
      trimmed = line
      sub(/^[ \t]+/, "", trimmed)
      if (trimmed == marker) {
        in_heredoc = 0
        marker = ""
        # Keep the marker line itself (its text is not user content)
        print line
      }
      # else: drop the heredoc body line
      next
    }
    # Detect heredoc opener anywhere on the line:
    #   <<[-]? optional whitespace, optional quote (single or double), MARKER, optional close-quote
    if (match($0, /<<-?[[:space:]]*[\x27"]?[A-Za-z_][A-Za-z0-9_]*[\x27"]?/)) {
      m = substr($0, RSTART, RLENGTH)
      # Strip the <<, optional dash, whitespace, and any surrounding quotes
      sub(/^<<-?[[:space:]]*[\x27"]?/, "", m)
      sub(/[\x27"]?$/, "", m)
      marker = m
      in_heredoc = 1
      print $0
      next
    }
    print $0
  }
' | sed "s/\"[^\"]*\"//g; s/'[^']*'//g")

# Resolve which directory the command actually ran in:
#   1. tool_input.cwd if explicitly set
#   2. Otherwise the hook's own cwd (legacy behavior)
if [ -n "$TOOL_CWD" ]; then
  TOOL_CWD="${TOOL_CWD/#\~/$HOME}"
  COMMAND_CWD="$TOOL_CWD"
else
  COMMAND_CWD="$(pwd)"
fi

# Walk up from COMMAND_CWD to find the nearest .beacon/project.json
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

BEACON_ROOT=$(find_beacon_root "$COMMAND_CWD")
if [ -z "$BEACON_ROOT" ]; then
  _hook_debug "no-root" "" "$CMD"
  exit 0
fi

emit() {
  local msg="$1"
  printf '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"%s (project: %s)"}}' \
    "$msg" "$BEACON_ROOT"
}

# e-4080: the two review 節目 grep patterns below are the SINGLE SOURCE'd copy of
# lib/review_nodes.py REVIEW_NODES[*].grep_pattern. Keep them byte-identical —
# tests/test_review_node_manifest_drift.py fails the build if they diverge (a
# silent wake loss is exactly the drift this review machinery exists to prevent).
SKILL=""
NODE=""
if echo "$CMD_BARE" | grep -qE 'git commit '; then
  emit "BEACON: Commit detected. You MUST now run /beacon-log Skill to record this commit."
  SKILL="/beacon-log"
elif echo "$CMD_BARE" | grep -qE 'git push'; then
  emit "BEACON: Push detected. You MUST now run /beacon-push Skill to record this push."
  SKILL="/beacon-push"
elif echo "$CMD_BARE" | grep -qE 'beacon pr (add|create)|gh pr create'; then  # review_nodes.py id=pr-open
  # ms-119 e-4060: a PR-open is a review 節目. Before this branch the review-due
  # trigger fired into a file but nothing WOKE the AI to consume it (unlike
  # commit/push/deploy, which each have a MUST-run wake here) — so AX /
  # maintainability reviews fired into a void and were skipped. This closes that
  # gap by giving PR-open the same forcing-function wake.
  emit "BEACON: PR opened. AX + maintainability review are due (文脈ゼロの独立 judge に原典+差分を渡す節目). まず 'beacon trigger check' を実行し、出てくる ax-review-due / maintainability-review-due トリガーの message から実際の PR 番号を得る (message に PR# が入っている)。次にその PR# で /beacon-review-run を 2 回、別々に実行する: (1) /beacon-review-run --type ax --pr <PR#>、続けて (2) /beacon-review-run --type maintainability --pr <PR#>。--type both は ax+philosophy を指し maintainability を含まないので使わないこと。approve/merge は両レビュー実施まで beacon pr approve でブロックされる。"
  SKILL="/beacon-review-run"
  NODE="pr-open"
elif echo "$CMD_BARE" | grep -qE 'beacon (milestone (done|close|observe)|operation close)( |$)'; then  # review_nodes.py id=target-close
  # ms-119 e-4077: closing a target (milestone / operation) is the OTHER review
  # 節目 — it fires 思想 (philosophy) + 目的達成 (attainment) review-due (see
  # commands.py _fire_review_due_trigger). e-4060 only wired the PR-open wake, so
  # this transition path fired into the same void. Same forcing-function wake:
  # check the fired triggers and run the review. Routine/reversible transitions
  # fire nothing, so `trigger check` just shows no review-due — no false nag.
  # `milestone observe` is included because observing is a completion claim in
  # this codebase (運用改善フェーズ = 基本目的達成が前提), not a reversible pause.
  emit "BEACON: Target close command detected (成否は未確認). 思想(philosophy) + 目的達成(attainment) review may be due. まず 'beacon trigger check' を実行する。<type>-review-due トリガーが出ていれば、その message に対象 target-id が入っている。出ていれば 2 つを実行する: (1) /beacon-review-run --type philosophy --target <target-id> で思想レビュー、続けて (2) 'beacon target review-request <target-id>' で目的達成の証拠を集めて人間承認へ (SPEC 方針2: attainment verdict は人間所有)。review-due トリガーが無ければ何もしなくてよい (close 失敗 / 誤マッチ含む)。"
  SKILL="/beacon-review-run"
  NODE="target-close"
elif echo "$CMD_BARE" | grep -qE 'gcloud run deploy|gcloud app deploy|scripts/deploy\.sh'; then
  emit "BEACON: Deploy detected. You MUST now run /beacon-deploy Skill to record this deployment."
  SKILL="/beacon-deploy"
elif echo "$CMD_BARE" | grep -qE 'aws s3 sync.*s3://|terraform apply|aws cloudfront create-invalidation'; then
  emit "BEACON: Deploy detected. You MUST now run /beacon-deploy Skill to record this deployment."
  SKILL="/beacon-deploy"
elif echo "$CMD_BARE" | grep -qE 'vercel( --prod| deploy)|firebase deploy|fly deploy|flyctl deploy|netlify deploy'; then
  emit "BEACON: Deploy detected. You MUST now run /beacon-deploy Skill to record this deployment."
  SKILL="/beacon-deploy"
elif echo "$CMD_BARE" | grep -qE 'kubectl apply|cdk deploy|serverless deploy|sls deploy|pulumi up|eb deploy|az webapp deploy|az functionapp'; then
  emit "BEACON: Deploy detected. You MUST now run /beacon-deploy Skill to record this deployment."
  SKILL="/beacon-deploy"
fi

if [ -n "$SKILL" ]; then
  # e-4080: append the review-node id (pr-open / target-close) when set, so the
  # debug log distinguishes the two review 節目 that share /beacon-review-run.
  _hook_debug "matched:$SKILL${NODE:+:$NODE}" "$BEACON_ROOT" "$CMD"
else
  _hook_debug "no-match" "$BEACON_ROOT" "$CMD"
fi
