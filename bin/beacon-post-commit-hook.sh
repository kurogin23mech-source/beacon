#!/bin/bash
# PostToolUse hook: detect git commit or deploy and prompt Claude to run beacon skills
# Called by Claude Code with JSON on stdin

INPUT=$(cat /dev/stdin)
CMD=$(echo "$INPUT" | jq -r '.tool_input.command // ""')
STDOUT=$(echo "$INPUT" | jq -r '.tool_response.stdout // ""')

if echo "$CMD" | grep -qE 'git commit ' && [ -f .beacon/project.json ]; then
  printf '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"BEACON: Commit detected. You MUST now run /beacon-log Skill to record this commit."}}'
elif echo "$CMD" | grep -qE 'git push' && [ -f .beacon/project.json ]; then
  printf '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"BEACON: Push detected. You MUST now run beacon push record to record this push."}}'
elif echo "$CMD" | grep -qE 'gcloud run deploy|gcloud app deploy' && echo "$STDOUT" | grep -q 'Done\.' && [ -f .beacon/project.json ]; then
  printf '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"BEACON: Deploy detected. You MUST now run /beacon-deploy Skill to record this deployment."}}'
fi
