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

- **Always-visible dashboard** — Tauri Desktop App or Web UI shows live progress alongside your working shell
- **Audit trail** — every commit and task is recorded under a milestone, making AI session handoffs transparent and human-auditable
- **CLI-first design** — structured JSON output enables seamless integration with Claude Code Skills

## Requirements

- **Python 3.9+** (CLI core)
- **Git** (for commit tracking and worktree dispatch)

## Installation

Beacon ships in two forms that work independently:

- **Beacon Desktop** — Tauri-based UI app (`.dmg` / `.msi` / `.AppImage`) you double-click. CLI is bundled inside.
- **Beacon CLI** — command-line tool (`beacon`) for terminal users, AI agents, and CI.

### Quick install — pick your OS

| OS | Beacon Desktop | Beacon CLI |
|---|---|---|
| **macOS** | `brew install --cask beacon-desktop`<br>or download `.dmg` from [Releases](https://github.com/r-kida2/beacon/releases) | `brew install r-kida2/beacon/beacon`<br>or `pipx install beacon` |
| **Windows** | `winget install BeaconAI.BeaconDesktop`<br>or download `.msi` from [Releases](https://github.com/r-kida2/beacon/releases) | `pipx install beacon` |
| **Linux** | Download `.AppImage` from [Releases](https://github.com/r-kida2/beacon/releases) and `chmod +x` | `pipx install beacon`<br>or Homebrew on Linux |

`pipx install beacon` works natively on all three OSes — PowerShell-only Windows users get the full CLI without WSL2.

For cloud features (Google auth, team collaboration), add the `cloud` extra:

```bash
pipx install 'beacon[cloud]'
```

See [INSTALL.md](INSTALL.md) for full details: tap setup for maintainers, code-signing status, SmartScreen workarounds, troubleshooting per OS.

## Quick Start

Run the setup wizard from your project directory:

```bash
cd your-project
beacon setup
```

`beacon setup` handles everything in one go:
1. **Sign in** with Google (for cloud features)
2. **Install** Claude Code Skills and hooks
3. **Initialize** a new local project — or **join** an existing cloud project
4. **Launch** the UI (Tauri Desktop App or Web UI)

### Path A: New local project

Choose option `1` in step 3. The wizard prompts for a project name and objective, then shows your status and UI pointers.

```bash
beacon setup
# → Step 3: Choose [1] New local project
# → Step 4: Show status + UI pointers? [Y]
beacon milestone add "First milestone"
beacon milestone start ms-1
```

For live progress visualization, use the [Tauri Desktop App](#desktop-app) or [Web UI](https://beacon-ai.dev) (after `beacon cloud push`).
The bare `beacon` command prints `beacon status` plus pointers to both front-ends.

To push to cloud later for Web UI access and team collaboration:

```bash
beacon cloud push
```

### Path B: Join an existing cloud project (invited)

Choose option `2` in step 3 and enter the project ID shared by your team.

```bash
beacon setup
# → Step 3: Choose [2] Join a cloud project
# → Cloud project ID: your-project-abc123
```

Or skip the wizard and join directly:

```bash
beacon auth login
beacon cloud join your-project-abc123
beacon
```

> **Windows**: Use `beacon auth login` first (opens a browser). The rest of the commands work the same in PowerShell or CMD.

### Desktop App (optional)

The [Desktop App](#desktop-app) provides a native window for monitoring your project alongside your terminal.

### Multi-session DM (preview)

Beacon includes a real-time DM channel that lets parallel Claude Code
sessions talk to each other — across machines, across worktrees, across
projects. Open the same project on your Mac and your Windows box, and
the two sessions can directly hand off context without you copy-pasting.

`beacon setup` enables this automatically; `bclaude` is the recommended
launcher (it's the regular `claude` command with the DM channel wired
up). Don't want it? `beacon channel opt-out` keeps it off — globally
or per-project.

```bash
bclaude                          # launch Claude Code with DM enabled
beacon bus directory --live      # see other live sessions in the project
beacon channel status            # check install / opt-out state
beacon channel opt-out --global  # disable everywhere, forever
```

See [`docs/getting-started-dm.md`](docs/getting-started-dm.md) for the
full walkthrough.

#### Forking a parallel session

When you want a second bclaude in a separate worktree — e.g. to give a
"difficult" milestone its own interactive session while you keep working
on the current one — use the `/beacon-session-fork` Skill, or call its
CLI helper directly:

```bash
beacon session fork ms-12        # creates .worktrees/ms-12-fork-<id>
                                 # + .beacon/cloud.json + channel install
                                 # + .beacon/fork.json (parent ↔ child link)
# then in a new terminal:
cd .worktrees/ms-12-fork-<id> && bclaude
```

The Skill (`/beacon-session-fork ms-12`) also auto-opens a new
Terminal.app / iTerm2 window on macOS and starts `bclaude` for you.

## Cloud Mode

Beacon can sync to the cloud for Web UI access at **https://beacon-ai.dev** and team collaboration. All CLI commands (`beacon log`, `beacon task`, etc.) automatically work through the cloud API.

### Team collaboration

The project owner can invite members from the Web UI (hamburger menu → Members). Members need to sign in at beacon-ai.dev first, then join via CLI.

| Role | Read | Write | Manage members |
|------|------|-------|----------------|
| owner | ✓ | ✓ | ✓ |
| editor | ✓ | ✓ | — |
| viewer | ✓ | — | — |

### Cloud commands

| Command | Description |
|---------|-------------|
| `beacon auth login` | Sign in with Google / Googleログイン |
| `beacon auth status` | Show login status / ログイン状態 |
| `beacon cloud push` | Upload project to cloud / クラウドにアップロード |
| `beacon cloud pull` | Download project from cloud / クラウドからダウンロード |
| `beacon cloud join <id>` | Join an existing cloud project / 既存プロジェクトに参加 |
| `beacon cloud list` | List cloud projects / クラウドプロジェクト一覧 |
| `beacon cloud status` | Show cloud config / クラウド設定表示 |
| `beacon cloud off` | Switch back to local mode / ローカルモードに戻す |

## CLI Commands

### Project

| Command | Description |
|---------|-------------|
| `beacon` | Show status + pointers to Tauri Desktop App / Web UI / ステータス+UIリンク表示 |
| `beacon setup` | First-time setup wizard (auth + hooks + project) / 初回セットアップ |
| `beacon init [--name n] [--objective o] [--storage local\|cloud]` | Initialize `.beacon/` (flags for non-interactive use) / プロジェクト作成 |
| `beacon status [--json]` | Show project status / ステータス表示 |
| `beacon skill install` | Install Claude Code Skills / Skillインストール |
| `beacon monitor context [--dry-run]` | Run the Stop hook context-usage threshold monitor (debug / 動作確認) |
| `beacon doctor` | Check PATH, hooks, Skills, auth, and cloud config / 環境ヘルスチェック |
| `beacon reset` | Move `.beacon/` to a timestamped backup (local only) / ローカルデータのリセット |

### Milestones

| Command | Description |
|---------|-------------|
| `beacon milestone add "title" [-d date]` | Add milestone / 追加 |
| `beacon milestone list` | List milestones / 一覧 |
| `beacon milestone start <id>` | Activate + auto-create `ms-XX-<slug>` branch + self-add as assignee (use `--no-branch` / `--no-assignee` to opt out) / アクティブ化＋ブランチ自動作成＋自己 assignee 登録 |
| `beacon milestone join <id> [--checkout]` | Add self as assignee (and optionally switch branch) / 他の MS に参加 |
| `beacon milestone done <id>` | Mark as done / 完了 |
| `beacon milestone close <id>` | Close milestone / クローズ |
| `beacon milestone observe <id>` | Set to observing / 監視中に設定 |
| `beacon milestone show <id> [--json]` | Show details / 詳細表示 |
| `beacon milestone update <id> [options]` | Update fields / 更新 |
| `beacon milestone delete <id>` | Logical delete (cancelled) / 論理削除 |
| `beacon milestone purge <id> --reason "..." [--index <n>]` | Hard delete a milestone record — recovery for duplicate-ID corruption (Issue #14) / ハード削除（重複ID復旧用） |
| `beacon milestone rename <id> "new title"` | Rename milestone / 名前変更 |
| `beacon milestone depends <id> --on <id>[,id]` | Set dependencies / 依存関係設定 |
| `beacon milestone graph [--json]` | Show dependency graph / 依存グラフ表示 |
| `beacon milestone workspace <id> [--executor ai\|human]` | Create git worktree for isolated development / worktree作成 |
| `beacon milestone workspace-cleanup <id>` | Remove worktree after work completes / worktree削除 |

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
| `beacon entry purge <e-id> --reason "..." [--index <n>]` | Hard delete an entry record — recovery for duplicate-ID corruption (e-863) / ハード削除（重複ID復旧用） |

### Pull Requests

| Command | Description |
|---------|-------------|
| `beacon pr create [-m ms-id] [--intent "text"] [gh flags...]` | Run `gh pr create` and record the PR / PR作成+自動記録 |
| `beacon pr add <github-url> [-m ms-id] [--intent "text"]` | Register an existing PR / 既存PRを登録 |
| `beacon pr approve <entry-id> [--rationale "text"]` | Approve a PR (rationale required) / 承認 |
| `beacon pr request-changes <entry-id> [--rationale "text"]` | Request changes / 修正依頼 |
| `beacon pr reject <entry-id> [--rationale "text"]` | Reject a PR / 却下 |
| `beacon pr merge <entry-id>` | Mark as merged / マージ済みに設定 |
| `beacon pr close <entry-id>` | Close without merging / クローズ |

Use `/review` Claude Code Skill instead of `beacon pr review` for AI-assisted code review.

### Deploy

| Command | Description |
|---------|-------------|
| `beacon deploy record [--revision <rev>] [--semver <v1.0.0>] [--desc "text"]` | Record a deployment (auto major/minor detection) / デプロイ記録 |
| `beacon deploy list [--json]` | List deployment history / デプロイ履歴一覧 |

`beacon deploy record` automatically determines **major** (one or more milestones newly completed) vs **minor** (bug fix / patch to already-shipped milestones) based on commit history. The `/beacon-deploy` Skill wraps this with a two-phase prepare/finalize flow for AI-generated deploy descriptions.

### Code Releases (Push Log)

| Command | Description |
|---------|-------------|
| `beacon push record [--desc "text"]` | Record a git push (auto-collects commits since last push) / pushを記録 |
| `beacon push list [--json]` | List push history / push履歴一覧 |

The `/beacon-push` Skill wraps push recording with AI-generated value descriptions, triggered automatically by the PostToolUse hook after each `git push`.

### GitHub Issues

| Command | Description |
|---------|-------------|
| `beacon issue import <number> [-m ms-id]` | Import a GitHub Issue as a beacon task / IssueをBeaconタスクとして取り込み |
| `beacon issue sync [-m ms-id]` | Bulk import all open Issues not yet imported / 未インポートIssueを一括取り込み |
| `beacon issue list [--json]` | List open Issues not yet imported / 未インポートIssue一覧 |

Requires `gh` CLI authenticated. When a task linked to an Issue is marked done, beacon suggests closing the Issue via `gh issue close`.

### Documents

| Command | Description |
|---------|-------------|
| `beacon doc add "title" [--scope core\|spec\|memo] [--ms ms-id] [--op op-id] [--content text]` | Add document / ドキュメント追加 |
| `beacon doc list [--scope scope] [--ms ms-id] [--op op-id] [--json]` | List documents / 一覧 |
| `beacon doc show <doc-id>` | Show document / 内容表示 |
| `beacon doc update <doc-id> --content "text"` | Update document / 更新 |

Scopes: `core` (design principles / 設計原則), `spec` (technical specs / 仕様), `memo` (notes / メモ)

Use `--ms <ms-id>` to associate a document with a milestone, or `--op <op-id>` to associate with an Operation (e.g. log fetch instructions for `/beacon-operation-review`).

### Save (Non-commit actions)

| Command | Description |
|---------|-------------|
| `beacon save "desc" -m ms-id [--source src]` | Record a non-commit action / コミット以外の行為を記録 |
| `beacon save "desc" -m ms-id --hash <hash>` | Link save to a related commit / 関連コミットに紐づけ |
| `beacon save "desc" --source google_docs --url "..."` | Record with external resource / 外部リソース付きで記録 |

Sources: `manual` (default, AI self-report), `google_docs`, `notion`, `jupyter`, or any custom string.

### Logging

| Command | Description |
|---------|-------------|
| `beacon log [message] [-m ms-id]` | Record HEAD commit to milestone / コミット記録 |
| `beacon log --prepare` | Output context as JSON (read-only) / 判断材料出力 |
| `beacon log --finalize --progress N` | Write evaluation results / 評価結果書き込み (`--summary` was retired in e-1040 — see `project-vision` doc + session log) |
| `beacon summary [--json]` | View project summary (read-only). Writes retired in e-1040 — use `project-vision` doc + `beacon session log` |
| `beacon sync` | Auto-sync recent commits / 直近コミットを同期 |
| `beacon search <query> [-m ms-id] [--json]` | Full-text search across milestones, tasks, commits, PRs, and saves / 全文検索 |

### Operations

Operations track recurring operational workloads (daily batch jobs, incident management) — the "maintenance" layer alongside development Milestones.

| Command | Description |
|---------|-------------|
| `beacon operation open "title" [--schedule daily\|weekdays\|weekly] [--log-source name]` | Start a new Operation cycle / 新しいOperationを開始 |
| `beacon operation close <op-id>` | Close an Operation cycle / クローズ |
| `beacon operation purge <op-id> --reason "..." [--index <n>]` | Hard delete an operation record — recovery for duplicate-ID corruption (e-863) / ハード削除（重複ID復旧用） |
| `beacon operation list [--json]` | List Operations / 一覧 |
| `beacon operation show <op-id> [--json]` | Show Operation with entries + active envelope / 詳細表示（有効な envelope 含む） |
| `beacon operation approve <op-id> --spec <doc-id> [--expires-at YYYY-MM-DD] [--json]` | Mint T2 envelope from SPEC doc's `approved_actions` (ms-60) / SPEC ドキュメントの許可 action 一覧から T2 envelope を発行 |
| `beacon operation revoke <op-id> [--envelope-id ENV] [--reason TEXT] [--json]` | Invalidate the active envelope (or a specific one) / 有効な envelope を無効化 |
| `beacon operation envelope verify <op-id> <action> [--json]` | AI self-check used by `/beacon-operation-execute` — is the action permitted by the active envelope? (ms-60 / e-1340) / 指定 action が envelope の許可範囲か判定 |
| `beacon run record -o <op-id> --batch <name> --status ok\|warning\|error --desc "..."` | Record a batch run result / バッチ実行結果を記録 |
| `beacon run list -o <op-id> [--json]` | List run records / 実行記録一覧 |
| `beacon incident open "title" -o <op-id> [--desc "..."]` | Open an incident / インシデント起票 |
| `beacon incident close <id> --resolution "..."` | Resolve an incident / インシデント解決 |
| `beacon incident escalate <id> -m <ms-id>` | Escalate incident to a Milestone task / Milestoneタスクに昇格 |

The `/beacon-operation-setup` Skill walks through setup conversationally and auto-generates a SPEC document (log fetch instructions). The `/beacon-operation-review` Skill reads that SPEC, fetches logs, interprets them, and records the result — triggered automatically by `operation_check_<op-id>` triggers at session start.

### Session Notes

Ephemeral memos that survive context compaction within a session — cleared at session end.

| Command | Description |
|---------|-------------|
| `beacon note "text" [--context "label"]` | Add a session note / セッションメモ追加 |
| `beacon note list [--json]` | Show session notes / メモ一覧 |
| `beacon note clear` | Clear all session notes / 全削除 |

Say "メモして" or "remember this" and Claude will call `/beacon-note` automatically. At session end, `/beacon-session-end` prompts to promote important notes to permanent Documents before clearing.

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

### DM Channel (multi-session messaging)

Manage the `beacon-bus` MCP channel that powers parallel-session DM. See
[`docs/getting-started-dm.md`](docs/getting-started-dm.md) for the full
walkthrough.

| Command | Description |
|---------|-------------|
| `beacon channel install` | Install beacon-bus MCP entry into `.mcp.json` / DM 機能を有効化 |
| `beacon channel uninstall [--purge-files\|--keep-files]` | Remove MCP entry; `--purge-files` also moves `channel/node_modules` to `.trash/` |
| `beacon channel opt-out [--project\|--global]` | Block install / auto-install (persistent flag) |
| `beacon channel opt-in [--project\|--global]` | Lift the opt-out flag |
| `beacon channel status` | Show install / files / opt-out / next-action state |

`bclaude` (shipped alongside `beacon` in Homebrew) launches Claude Code
with the channel pre-wired, and falls back to plain `claude` when any
opt-out source is active.

## Dashboard

Beacon's visual UI is split across two front-ends:

- **[Tauri Desktop App](#desktop-app)** — cross-platform native window for monitoring milestones, tasks, and entries.
- **[Web UI](https://beacon-ai.dev)** — browser-based dashboard for cloud-linked projects (after `beacon cloud push`).

The legacy curses dashboard (`lib/dashboard.py`, tmux split-pane) was retired in e-764 and archived to `.trash/lib-dashboard-py-e764/`. The bare `beacon` command now prints status plus pointers to the two supported UIs.

## Claude Code Integration

Beacon ships with [Claude Code Skills](https://docs.anthropic.com/en/docs/claude-code) for automated project tracking. `beacon setup` installs them automatically; you can also run `beacon skill install` at any time to update.

| Skill | Description |
|-------|-------------|
| `beacon-session-start` | Restore project context at session start / セッション開始時にコンテキスト復元 |
| `beacon-log` | Record commits with AI-evaluated progress and summary / コミット記録+AI進捗評価 |
| `beacon-task` | Task CRUD operations / タスク操作 |
| `beacon-session-end` | Update summary and organize open tasks / サマリー更新+未完了整理 |
| `beacon-deploy` | Record a deployment with AI-generated description / AI説明付きデプロイ記録 |
| `beacon-push` | Record a git push with AI-generated value description / AI説明付きpush記録 |
| `beacon-retro` | Generate and discuss weekly retrospective / 週次振り返りドキュメント生成 |
| `beacon-dispatch` | Identify executable milestones and launch parallel sub-agents / 並列サブエージェント起動 |
| `beacon-init` | Conversational project initialization with Project Archaeology / 会話形式プロジェクト初期化 |
| `beacon-note` | Record an ephemeral session memo (auto-triggered by "メモして") / セッションメモ記録 |
| `beacon-operation-setup` | Conversational Operation setup with auto-generated SPEC doc / 会話形式でOperation作成+SPEC自動生成 |
| `beacon-operation-review` | Fetch logs per SPEC, interpret, and record run result / SPECに従いログ取得→解釈→run record記録 |
| `beacon-spec` | Conversational SPEC (requirements / decision trail) authoring + task breakdown / 対話駆動SPEC作成+タスク一括起票 |
| `beacon-retrospect` | Query project history in natural language ("did we build X? how?") / プロジェクト史を自然言語で検索 |
| `beacon-vision` | Refine a fuzzy idea into a structured project vision (CORE doc) / ふわっとした構想をビジョンに精緻化 |
| `beacon-roadmap` | Design a set of milestones (3–7) toward the project vision / 大目的に向けたマイルストーン群を設計 |
| `beacon-pr-create` | Conversational PR creation (intent capture + branch/commit checks) / 対話形式でPR作成 |
| `beacon-incident-report` | Close an incident and save a cause/response/prevention report / インシデントclose+レポート作成 |

### Two-phase workflow

Skills use a **prepare/finalize** pattern to keep AI judgment structured:

1. `beacon log --prepare` (or `beacon deploy record --prepare`, `beacon push record --prepare`) outputs context as JSON
2. The Skill prompts Claude to generate a summary, deploy description, or push description
3. `beacon log --finalize` (or `beacon deploy record --finalize`, `beacon push record --desc "..."`) writes the result

This design ensures AI-generated content is channeled through deterministic CLI operations, not free-form file edits.

## Desktop App

Beacon includes a native desktop application built with [Tauri](https://tauri.app/) for local, cross-platform project monitoring without a browser.

```bash
cd desktop
npm install
npm run tauri dev   # Development
npm run tauri build # Production (.app/.dmg on macOS)
```

The Desktop App uses the same beacon CLI as its backend and supports both local and cloud projects. It features the same tabs as the Web UI: Milestones, Graph, Reviews, and Documents.

## Data Model

All project state lives in `.beacon/project.json`. See [SPEC.md](docs/SPEC.md) for the full schema.

```
.beacon/
  project.json    # Project state (milestones, entries, summary)
  config.json     # Mode config: local or cloud
  cloud.json      # Cloud project binding (project_id, api_url)
  documents/      # Project documents (Markdown with YAML frontmatter)
  retro/          # Weekly retrospective documents
  triggers/       # Async message queue (dashboard <-> Claude Code)
```

## Doc & Skill auto-sync scope (ms-10 e-722)

To prevent doc drift, three guards run automatically (pre-commit hook + CI
`lint-docs` workflow):

| Guard | What it checks |
|-------|----------------|
| `scripts/check-skill-drift.py` | `skills/*.md` in the repo matches `~/.claude/skills/<name>/SKILL.md`. Repair: `beacon skill install` (or set `BEACON_AUTO_SKILL_INSTALL=1` to auto-run from the hook). Also surfaced by `beacon doctor` check #6. |
| `scripts/check-cli-help-drift.py` | The set of `(subcommand, subsubcommand)` pairs is consistent across `bin/beacon` usage, `cmd_help_json` (`beacon help --json`), and the README `## CLI Commands` tables. Intentional asymmetries are listed inline as allowlists. |
| `scripts/check-install-md.py` | `INSTALL.md` bash blocks don't use deprecated flags (e.g. `gcloud run deploy --build-arg` — gcloud wants `--set-build-env-vars`). Brew / pipx commands have a package positional. |

**Explicitly out of scope** (these still need a human reviewer):

- Whether a CLI flag's **description text** is accurate — the guard only
  checks that the flag exists in `--help`, not what it means.
- Whether the README's prose paragraphs (Why Beacon?, Quick Start, etc.)
  reflect the latest behavior — only the CLI tables are diffed.
- Skill body content review — drift detection only catches "you forgot to
  run `beacon skill install`", not "this Skill's instructions became wrong".

Catch-up sweeps (manual doc audits) are still useful when the scope
above misses something, but the day-to-day drift surface should now stay
clean by construction.

## License

[MIT](LICENSE)
