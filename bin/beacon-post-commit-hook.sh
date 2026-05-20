#!/bin/bash
# PostToolUse hook: detect git commit or deploy and prompt Claude to run beacon skills
# Called by Claude Code with JSON on stdin

INPUT=$(cat /dev/stdin)
CMD=$(echo "$INPUT" | jq -r '.tool_input.command // ""')

# Strip content inside quotes to avoid matching patterns in --summary or --desc arguments
CMD_BARE=$(echo "$CMD" | sed "s/\"[^\"]*\"//g; s/'[^']*'//g")

if echo "$CMD_BARE" | grep -qE 'git commit ' && [ -f .beacon/project.json ]; then
  printf '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"BEACON: Commit detected. You MUST now run /beacon-log Skill to record this commit."}}'
elif echo "$CMD_BARE" | grep -qE 'git push' && [ -f .beacon/project.json ]; then
  printf '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"BEACON: Push detected. You MUST now run /beacon-push Skill to record this push."}}'
elif echo "$CMD_BARE" | grep -qE 'gcloud run deploy|gcloud app deploy|scripts/deploy\.sh' && [ -f .beacon/project.json ]; then
  printf '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"BEACON: Deploy detected. You MUST now run /beacon-deploy Skill to record this deployment."}}'
elif echo "$CMD_BARE" | grep -qE 'aws s3 sync.*s3://|terraform apply|aws cloudfront create-invalidation' && [ -f .beacon/project.json ]; then
  printf '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"BEACON: Deploy detected. You MUST now run /beacon-deploy Skill to record this deployment."}}'
fi
