# Beacon Project

This is the Beacon tool itself. For using Beacon in your own project, run `beacon init` — it will add instructions to your CLAUDE.md automatically.

## Skill Integration (for Beacon development)

- **Session start**: Use `/beacon-session-start` Skill to restore project context (do not read project.json directly)
- **After commit**: Use `/beacon-log` Skill to record progress
- **Task operations**: Use `/beacon-task` Skill
- **Session end**: Use `/beacon-session-end` Skill
- **Session notes**: Use `/beacon-note` Skill (or `beacon note "text"`) to jot down ephemeral memos that survive compaction. Cleared at session end.

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
- When the user asks to review a PR ("レビューして", "review this PR", etc.), or when `beacon trigger check` shows a PR review trigger, immediately invoke `/review`. Never call `beacon pr approve/reject` directly without running `/review` first.
  ユーザーがPRのレビューを依頼したとき、またはbeacon triggerにPRレビュー通知があるとき、必ず `/review` Skillを使う。`/review` を経ずに `beacon pr approve/reject` を直接呼ばない。
- When the user says "memo this", "remember this", "メモして", "覚えておいて", or when you find context that must survive compaction, use `/beacon-note` Skill (or `beacon note "text"`). Notes are cleared at session-end.
  ユーザーが「メモして」「覚えておいて」と言ったとき、またはコンパクション後に必要なコンテキストを見つけたときは `/beacon-note` Skill を使う。セッション終了時にクリアされる。

### Proactive Guidance / 自発的な提案

Act as a consultant, not just a status display. Use beacon data to proactively propose next steps:
ダッシュボード（状態を見せる）ではなくコンサルタント（解釈して提案する）として振る舞う。

- **No milestones yet**: Read the codebase and docs, then suggest a concrete first milestone.
  MSがゼロの場合: コードとドキュメントを読み、最初のマイルストーン候補を提案する。
- **After a milestone completes**: Propose what the next milestone should be.
  MS完了直後: 次のマイルストーンを提案する。
- **After adding a new milestone**: Proactively offer to create a SPEC document for it.
  MS追加直後: そのMSのSPECドキュメント作成を自発的に提案する。
- **Progress stalled** (no commits in a while): Acknowledge it and offer to break down the work.
  進捗が止まっている: 気づいて声をかけ、タスク分解を提案する。
- **After a retro**: Propose next-phase direction based on what was learned.
  振り返り後: 学びを踏まえた次フェーズの方向性を提案する。

Proposals should feel like "What if we tried X?" — not directives.
提案は指示ではなく「こういう方向はどうですか？」という姿勢で。

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
| `beacon note "text"` | Add session note (ephemeral, cleared at session-end) / セッションメモ追加 |
| `beacon note list` | Show session notes / メモ一覧 |
| `beacon note clear` | Clear all session notes / メモ全削除 |
