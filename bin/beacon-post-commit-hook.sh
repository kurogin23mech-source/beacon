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

# Strip content inside quotes to avoid matching patterns in --summary or --desc arguments
CMD_BARE=$(echo "$CMD" | sed "s/\"[^\"]*\"//g; s/'[^']*'//g")

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
