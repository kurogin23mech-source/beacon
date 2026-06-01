#!/bin/bash
# PostToolUse hook: detect git commit / push / deploy and prompt Claude to run beacon skills
# Called by Claude Code with JSON on stdin
#
# cwd-aware: respects tool_input.cwd so Claude Code launched in ~ can manage
# projects under ~/<name>/. Walks up from the resolved cwd to find the nearest
# .beacon/project.json.

INPUT=$(cat /dev/stdin)
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
[ -z "$BEACON_ROOT" ] && exit 0

emit() {
  local msg="$1"
  printf '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"%s (project: %s)"}}' \
    "$msg" "$BEACON_ROOT"
}

if echo "$CMD_BARE" | grep -qE 'git commit '; then
  emit "BEACON: Commit detected. You MUST now run /beacon-log Skill to record this commit."
elif echo "$CMD_BARE" | grep -qE 'git push'; then
  emit "BEACON: Push detected. You MUST now run /beacon-push Skill to record this push."
elif echo "$CMD_BARE" | grep -qE 'gcloud run deploy|gcloud app deploy|scripts/deploy\.sh'; then
  emit "BEACON: Deploy detected. You MUST now run /beacon-deploy Skill to record this deployment."
elif echo "$CMD_BARE" | grep -qE 'aws s3 sync.*s3://|terraform apply|aws cloudfront create-invalidation'; then
  emit "BEACON: Deploy detected. You MUST now run /beacon-deploy Skill to record this deployment."
elif echo "$CMD_BARE" | grep -qE 'vercel( --prod| deploy)|firebase deploy|fly deploy|flyctl deploy|netlify deploy'; then
  emit "BEACON: Deploy detected. You MUST now run /beacon-deploy Skill to record this deployment."
elif echo "$CMD_BARE" | grep -qE 'kubectl apply|cdk deploy|serverless deploy|sls deploy|pulumi up|eb deploy|az webapp deploy|az functionapp'; then
  emit "BEACON: Deploy detected. You MUST now run /beacon-deploy Skill to record this deployment."
fi
