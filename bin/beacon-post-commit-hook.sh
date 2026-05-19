#!/bin/bash
# PostToolUse hook: detect git commit or deploy and prompt Claude to run beacon skills
# Called by Claude Code with JSON on stdin

INPUT=$(cat /dev/stdin)
CMD=$(echo "$INPUT" | jq -r '.tool_input.command // ""')
STDOUT=$(echo "$INPUT" | jq -r '.tool_response.stdout // ""')

if echo "$CMD" | grep -qE 'git commit ' && [ -f .beacon/project.json ]; then
  printf '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"BEACON: Commit detected. You MUST now run /beacon-log Skill to record this commit."}}'
elif echo "$CMD" | grep -qE 'git push' && [ -f .beacon/project.json ]; then
  printf '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"BEACON: Push detected. Record this push in 3 steps: (1) run `beacon push record --prepare` to get context JSON, (2) generate a 1-2 sentence Japanese description of what was delivered to users/developers (value-based, not a commit list), (3) run `beacon push record --desc \"<description>\"` to save."}}'
elif echo "$CMD" | grep -qE 'gcloud run deploy|gcloud app deploy' && echo "$STDOUT" | grep -q 'Done\.' && [ -f .beacon/project.json ]; then
  printf '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"BEACON: Deploy detected. You MUST now run /beacon-deploy Skill to record this deployment."}}'
fi
