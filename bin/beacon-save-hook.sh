#!/bin/bash
# PostToolUse hook: detect MCP tool calls and auto-run beacon save
# Called by Claude Code with JSON on stdin
# Output ONLY to stderr — stdout goes into Claude Code context

set -e

# Exit early if no .beacon/project.json
if [ ! -f .beacon/project.json ]; then
  exit 0
fi

# Read stdin (JSON from Claude Code PostToolUse)
INPUT=$(cat /dev/stdin)
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty')

# If tool_name not in input, try alternate field
if [ -z "$TOOL_NAME" ]; then
  TOOL_NAME=$(echo "$INPUT" | jq -r '.tool // empty')
fi

if [ -z "$TOOL_NAME" ]; then
  exit 0
fi

# ---------------------------------------------------------------------------
# Determine source from tool name prefix
# ---------------------------------------------------------------------------
SOURCE=""
case "$TOOL_NAME" in
  mcp__google-drive__*)
    SOURCE="google_drive"
    ;;
  mcp__claude_ai_Google_Drive__*)
    SOURCE="google_drive"
    ;;
  *)
    # Not a monitored tool — exit silently
    exit 0
    ;;
esac

# ---------------------------------------------------------------------------
# Filter: only write operations (skip read/get/list/search)
# ---------------------------------------------------------------------------
# Extract the operation name (after last __)
OPERATION="${TOOL_NAME##*__}"

case "$OPERATION" in
  read*|get*|list*|search*|describe*|auth*|download*|export*)
    exit 0
    ;;
esac

# ---------------------------------------------------------------------------
# Extract info from tool input
# ---------------------------------------------------------------------------
TOOL_INPUT=$(echo "$INPUT" | jq -r '.tool_input // {} | tostring')
if [ "$TOOL_INPUT" = "{}" ] || [ "$TOOL_INPUT" = "null" ]; then
  TOOL_INPUT=$(echo "$INPUT" | jq -c '.tool_input // {}')
fi

# Try to extract a meaningful name/title from tool input
NAME=$(echo "$INPUT" | jq -r '.tool_input.title // .tool_input.name // .tool_input.fileName // .tool_input.file_name // .tool_input.documentTitle // empty' 2>/dev/null)

# Build description
if [ -n "$NAME" ]; then
  DESCRIPTION="[$OPERATION] $NAME"
else
  DESCRIPTION="[$OPERATION]"
fi

# Try to extract URL if available
URL=$(echo "$INPUT" | jq -r '.tool_input.url // .tool_input.fileUrl // .tool_input.file_url // empty' 2>/dev/null)

# ---------------------------------------------------------------------------
# Find active milestone (must be exactly one)
# ---------------------------------------------------------------------------
ACTIVE_MS_COUNT=$(jq '[.milestones[] | select(.status == "in_progress")] | length' .beacon/project.json 2>/dev/null)

if [ "$ACTIVE_MS_COUNT" != "1" ]; then
  # Ambiguous (0 or multiple active) — skip, let user handle manually
  exit 0
fi

ACTIVE_MS_ID=$(jq -r '[.milestones[] | select(.status == "in_progress")][0].id' .beacon/project.json 2>/dev/null)

if [ -z "$ACTIVE_MS_ID" ]; then
  exit 0
fi

# ---------------------------------------------------------------------------
# Find beacon command
# ---------------------------------------------------------------------------
BEACON_CMD=""
if command -v beacon &>/dev/null; then
  BEACON_CMD="beacon"
elif [ -x "$(dirname "$0")/beacon" ]; then
  BEACON_CMD="$(dirname "$0")/beacon"
else
  exit 0
fi

# ---------------------------------------------------------------------------
# Execute beacon save (errors are swallowed)
# ---------------------------------------------------------------------------
SAVE_ARGS=("$DESCRIPTION" -m "$ACTIVE_MS_ID" --source "$SOURCE" --json)
if [ -n "$URL" ]; then
  SAVE_ARGS+=(--url "$URL")
fi

$BEACON_CMD save "${SAVE_ARGS[@]}" >/dev/null 2>&1 || true
