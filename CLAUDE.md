# Beacon Project

This is the Beacon tool itself. For using Beacon in your own project, run `beacon init` — it will add instructions to your CLAUDE.md automatically.

## Skill Integration (for Beacon development)

- **Session start**: Use `/beacon-session-start` Skill to restore project context (do not read project.json directly)
- **After commit**: Use `/beacon-log` Skill to record progress
- **Task operations**: Use `/beacon-task` Skill
- **Session end**: Use `/beacon-session-end` Skill

## Development Rules (not covered by Skills)

- Manage milestones with `beacon milestone` commands directly (no Skill for this yet)
- If 2+ commits address the same issue, suggest grouping them into a task

## Beacon Project Management

Skills and hooks are installed via `beacon setup`. This marker prevents `beacon setup` from re-appending this section to the beacon repo's own CLAUDE.md.

- When the user wants to implement multiple milestones in parallel ("parallel", "sub-agents", "dispatch", etc.), run `/beacon-dispatch` Skill. Do not call the Agent tool directly.
  ユーザーが複数MSの並列実装を求めた場合（「パラレル」「サブエージェント」「並列」等）、必ず `/beacon-dispatch` Skill を実行する。
