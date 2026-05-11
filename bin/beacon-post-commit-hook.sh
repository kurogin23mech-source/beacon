#!/bin/bash
# PostToolUse hook: detect git commit and inject beacon-log prompt
# Called by Claude Code with JSON on stdin

INPUT=$(cat /dev/stdin)
CMD=$(echo "$INPUT" | jq -r '.tool_input.command')

if echo "$CMD" | grep -qE 'git commit ' && [ -f .beacon/project.json ]; then
  PREPARE=$(beacon log --prepare 2>/dev/null) || true
  if [ -n "$PREPARE" ]; then
    printf '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"BEACON: Commit detected. You MUST now run /beacon-log Skill to record this commit with AI-evaluated progress and summary.\\nPrepare context:\\n%s"}}' "$PREPARE"
  fi
fi
