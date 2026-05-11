# Beacon

**Milestone-driven project dashboard for AI-assisted development**

AIアシスタント（Claude Code等）との開発セッションにおいて、マイルストーンベースのプロジェクト進捗を常時可視化し、方向性を見失わずに実装を進めるためのツール。

A real-time project dashboard that keeps milestone-based progress visible during AI-assisted development sessions (e.g., Claude Code), so you never lose sight of the bigger picture.

![Beacon Dashboard with Claude Code](docs/images/dashboard.jpg)

## Why Beacon?

**Beacon is not a task management tool.** If you need to plan tasks upfront and check them off, use Notion or Linear.

Beacon is built for a different workflow: **commit-driven, milestone-oriented development**. You set a milestone (the destination), then each developer — human or AI — autonomously decides what to do next, commits, and records progress. Tasks emerge from the work, not the other way around.

This matters especially in AI-assisted development, where sessions are short-lived and context is easily lost. Beacon keeps the bigger picture visible so that each session can pick up where the last one left off.

**Beaconはタスク管理ツールではありません。** タスクを事前に起票して消化していくなら、NotionやLinearを使ってください。

Beaconが想定するワークフローは**コミット駆動・マイルストーン指向の開発**です。まずマイルストーン（目的地）を設定し、開発者 — 人間でもAIでも — が自律的に次にやるべきことを判断し、コミットし、進捗を記録します。タスクは作業から生まれるものであり、事前に計画するものではありません。

これはAI駆動開発において特に重要です。セッションは短命でコンテキストは失われやすい。Beaconは全体像を常に可視化し、次のセッションが前回の続きからスムーズに再開できるようにします。

### Key Features

- **Always-visible dashboard** — tmux split-pane shows live progress alongside your working shell
- **Audit trail** — every commit and task is recorded under a milestone, making AI session handoffs transparent and human-auditable
- **CLI-first design** — structured JSON output enables seamless integration with Claude Code Skills

## Requirements

- Python 3.8+
- tmux
- Git

## Installation

```bash
# Clone the repository
git clone https://github.com/r-kida2/beacon.git

# Add to PATH
export PATH="$PATH:$(pwd)/beacon/bin"
```

Add the `export` line to your `~/.zshrc` or `~/.bashrc` to make it permanent.

## Quick Start

```bash
# Initialize beacon in your project
cd your-project
beacon init

# Add a milestone
beacon milestone add "Implement user auth" -d 2026-06-01

# Activate the milestone
beacon milestone start ms-1

# Launch the dashboard (tmux)
beacon
```

This opens a tmux session with the dashboard on the left (33%) and your working shell on the right (67%). Run `claude` in the right pane to start an AI-assisted session with full project visibility.

```
+------------------+----------------------------------------+
| Dashboard (33%)  |  Working Shell (67%)                   |
| live progress    |  $ claude                              |
| milestones       |                                        |
| entries          |                                        |
+------------------+----------------------------------------+
```

## CLI Commands

### Project

| Command | Description |
|---------|-------------|
| `beacon` | Launch tmux dashboard + shell / ダッシュボード起動 |
| `beacon init` | Initialize `.beacon/` in current directory / 初期化 |
| `beacon status [--json]` | Show project status / ステータス表示 |

### Milestones

| Command | Description |
|---------|-------------|
| `beacon milestone add "title" [-d date]` | Add milestone / 追加 |
| `beacon milestone list` | List milestones / 一覧 |
| `beacon milestone start <id>` | Set as active (in_progress) / アクティブ化 |
| `beacon milestone done <id>` | Mark as done / 完了 |
| `beacon milestone close <id>` | Close milestone / クローズ |
| `beacon milestone observe <id>` | Set to observing / 監視中に設定 |
| `beacon milestone show <id> [--json]` | Show details / 詳細表示 |
| `beacon milestone update <id> [options]` | Update fields / 更新 |
| `beacon milestone delete <id>` | Logical delete (cancelled) / 論理削除 |

### Tasks & Entries

| Command | Description |
|---------|-------------|
| `beacon task add "desc" [-m ms-id] [-t type] [-d detail]` | Add task to milestone / タスク追加 |
| `beacon task done <id> [-p progress]` | Mark as done / 完了 |
| `beacon task list [-m ms-id] [--json]` | List tasks / 一覧 |
| `beacon task show <id> [--json]` | Show details / 詳細 |
| `beacon task detail <id> [text]` | View/update detail / 詳細テキスト表示・更新 |
| `beacon task update <id> [options]` | Update fields / 更新 |
| `beacon task delete <id>` | Logical delete / 論理削除 |
| `beacon entry move <id> -t <task-id>` | Move entry under a task / タスク配下に移動 |

### Logging

| Command | Description |
|---------|-------------|
| `beacon log [message] [-m ms-id]` | Record HEAD commit to milestone / コミット記録 |
| `beacon log --prepare` | Output context as JSON (read-only) / 判断材料出力 |
| `beacon log --finalize --progress N --summary "text"` | Write evaluation results / 評価結果書き込み |
| `beacon summary [text] [--json]` | View/update project summary / サマリー更新 |
| `beacon sync` | Auto-sync recent commits / 直近コミットを同期 |

### Retrospectives

| Command | Description |
|---------|-------------|
| `beacon retro [--since DATE] [--until DATE]` | Generate weekly retro data / 週次振り返りデータ生成 |
| `beacon retro done` | Mark current retro as reviewed / 振り返りレビュー済み |

### Triggers

| Command | Description |
|---------|-------------|
| `beacon trigger fire <name> [message]` | Fire a trigger (used by dashboard) / トリガー発火 |
| `beacon trigger check` | Check pending triggers (JSON) / 未処理トリガー確認 |
| `beacon trigger clear <name>` | Clear a specific trigger / トリガー消化 |

## Dashboard

The curses-based dashboard runs in the left tmux pane and auto-refreshes when `.beacon/project.json` changes.

**Keyboard shortcuts:**
| Key | Action |
|-----|--------|
| `j` / `↓` | Move down / 下移動 |
| `k` / `↑` | Move up / 上移動 |
| `Enter` / `Space` | Expand/collapse milestone / 展開・折りたたみ |
| `d` | Toggle done entries / 完了エントリの表示切替 |
| `r` | Toggle retro view / 振り返り表示の切替 |
| `q` | Quit (closes tmux session) / 終了 |

## Claude Code Integration

Beacon ships with [Claude Code Skills](https://docs.anthropic.com/en/docs/claude-code) for automated project tracking. Install the Skills from `~/.claude/skills/`:

| Skill | Description |
|-------|-------------|
| `beacon-session-start` | Restore project context at session start / セッション開始時にコンテキスト復元 |
| `beacon-log` | Record commits with AI-evaluated progress and summary / コミット記録+AI進捗評価 |
| `beacon-task` | Task CRUD operations / タスク操作 |
| `beacon-session-end` | Update summary and organize open tasks / サマリー更新+未完了整理 |

### Two-phase workflow

Skills use a **prepare/finalize** pattern to keep AI judgment structured:

1. `beacon log --prepare` outputs milestone state, task completion rates, and recent entries as JSON
2. The Skill prompts Claude to evaluate progress and generate a summary
3. `beacon log --finalize --progress N --summary "text"` writes the result

This design ensures AI-generated evaluations are channeled through deterministic CLI operations, not free-form file edits.

## Data Model

All project state lives in `.beacon/project.json`. See [SPEC.md](docs/SPEC.md) for the full schema.

```
.beacon/
  project.json    # Project state (milestones, entries, summary)
  retro/          # Weekly retrospective documents
  triggers/       # Async message queue (dashboard <-> Claude Code)
```

## License

[MIT](LICENSE)
