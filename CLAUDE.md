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

This project uses [Beacon](https://github.com/r-kida2/beacon) for milestone-driven progress tracking.
このプロジェクトは Beacon でマイルストーンベースの進捗管理を行っています。

### Rules / ルール

- **Never edit `.beacon/project.json` directly. Always use beacon CLI commands.**
  `.beacon/project.json` を直接編集しない。必ず beacon CLI を使うこと。
- Before starting work, check milestones (`beacon status`) and confirm which milestone the work targets.
  実装開始前にマイルストーンを確認し、どのマイルストーンに向かう作業かユーザーに確認すること。
- After committing, the PostToolUse hook will auto-trigger `/beacon-log` Skill for AI-evaluated progress recording.
  コミット後はPostToolUse hookが自動で `/beacon-log` Skillを起動し、AI評価付きで進捗を記録する。
- If 2+ commits address the same issue, suggest grouping them into a task.
  同じ課題に2回以上コミットが発生したら、タスクにまとめることを提案する。
- Update the project summary when direction changes: `beacon summary "text"`
  方向性が変わった時はサマリーを更新する。書くべきは経緯・判断・背景であり、進捗率やMS名ではない。
- When the user hints at ending the session, or before you suggest splitting/ending the session yourself, run `/beacon-session-end` Skill first.
  ユーザーがセッション終了を仄めかしたとき、または自分自身がセッション分割・終了を提案する前に、必ず `/beacon-session-end` Skill を実行する。
- When the user wants to implement multiple milestones in parallel ("parallel", "sub-agents", "dispatch", etc.), run `/beacon-dispatch` Skill. Do not call the Agent tool directly.
  ユーザーが複数MSの並列実装を求めた場合（「パラレル」「サブエージェント」「並列」等）、必ず `/beacon-dispatch` Skill を実行する。Agent toolを直接呼ばない。

### CLI Quick Reference

| Command | Description |
|---------|-------------|
| `beacon status` | Show project status / ステータス表示 |
| `beacon milestone add "title"` | Add milestone / MS追加 |
| `beacon milestone start <id>` | Activate milestone / MS開始 |
| `beacon task add "desc" -m <ms-id>` | Add task / タスク追加 |
| `beacon task done <id>` | Complete task / タスク完了 |
| `beacon log "summary"` | Record commit (auto via hook) / コミット記録（hook経由で自動） |
| `beacon summary "text"` | Update summary / サマリー更新 |
