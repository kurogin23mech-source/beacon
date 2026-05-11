#!/bin/bash
# PostToolUse hook: detect git commit and prompt Claude to run beacon-log
# Called by Claude Code with JSON on stdin

INPUT=$(cat /dev/stdin)
CMD=$(echo "$INPUT" | jq -r '.tool_input.command')

if echo "$CMD" | grep -qE 'git commit ' && [ -f .beacon/project.json ]; then
  printf '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"BEACON: Commit detected. You MUST now run /beacon-log Skill to record this commit."}}'
fi
