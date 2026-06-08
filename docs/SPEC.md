# Beacon Specification

## Overview

Beacon is a tool that keeps milestone-based project progress always visible during AI-assisted development, so developers never lose sight of their direction.

### Design Principles

- **Auditability**: Make AI session handoffs transparent so humans can track and audit progress
- **Milestone-driven**: All work is tied to milestones, making progress visible at a glance
- **Tool-first**: AI operations go through deterministic CLI commands, not free-form prompt instructions — only steps requiring judgment are delegated to the AI

## Architecture

```
beacon (bin/beacon)                   - CLI entrypoint (bash)
lib/commands.py                       - Subcommand implementation (Python)
lib/core.py                           - Pure business logic (validation, CRUD)
lib/dashboard.py                      - Real-time tmux dashboard (Python/curses)
lib/store.py                          - Storage abstraction (Protocol + factory)
lib/store_local.py                    - Local JSON file backend
lib/store_api.py                      - Cloud API backend (HTTP + WebSocket)
lib/api_client.py                     - HTTP client for cloud API
lib/ws_client.py                      - WebSocket client (stdlib only)
lib/auth.py                           - Google OAuth authentication
server/app.py                         - FastAPI cloud API server
server/firestore_client.py            - Firestore wrapper for API backend
desktop/                              - Tauri desktop app (Rust + Web UI)
.beacon/project.json                  - Project state file (JSON)
~/.claude/skills/beacon-*/SKILL.md    - Claude Code Skill definitions
```

### Layer Structure

```
┌─────────────────────────────────────────┐
│  Skill (outermost layer, thin wrapper)  │
│  beacon-session-start / beacon-log /    │
│  beacon-task / beacon-session-end       │
├─────────────────────────────────────────┤
│  beacon CLI (workflow control)          │
│  bin/beacon + lib/commands.py           │
│  - CRUD operations (deterministic)      │
│  - --prepare: output context as JSON    │
│  - --finalize: write AI-generated input │
├─────────────────────────────────────────┤
│  Store abstraction (lib/store.py)       │
│  - StoreLocal: .beacon/project.json    │
│  - StoreApi: Cloud API + WebSocket      │
│  Mode selected by .beacon/config.json   │
└─────────────────────────────────────────┘
```

**Principle**: Skills only bridge `--prepare` output to `--finalize` input. Business logic lives in the CLI. The storage layer is transparent — CLI commands work identically in local and cloud mode.

## Launch Flow

### Important: Using with Claude Code

When using Beacon's tmux dashboard alongside Claude Code, launch in this order:

1. Run `beacon` in the terminal — tmux session starts (left: dashboard, right: shell)
2. Run `claude` in the right pane (working shell)

**Warning**: Do not run `! beacon` (no arguments) from inside Claude Code. `tmux attach-session` will crash the Claude Code process.

To check status from within Claude Code, use `! beacon status`.

## CLI Commands

### Milestone Subcommands

| Command | Description | --json |
|---------|-------------|--------|
| `beacon milestone add "title" [-d date] [--description "desc"]` | Add a milestone | - |
| `beacon milestone list` | List milestones | - |
| `beacon milestone start <id>` | Set milestone as active | - |
| `beacon milestone done <id>` | Set milestone as done | - |
| `beacon milestone close <id>` | Close milestone (keeps progress) | - |
| `beacon milestone observe <id>` | Set milestone to observing | - |
| `beacon milestone show <id>` | Show milestone details | Yes |
| `beacon milestone update <id> [opts]` | Update fields | Yes |
| `beacon milestone delete <id>` | Logical delete (cancelled) | Yes |
| `beacon milestone rename <id> "new title"` | Rename a milestone | - |
| `beacon milestone depends <id> --on <id>[,id]` | Set milestone dependencies | Yes |
| `beacon milestone depends <id> --clear` | Remove all dependencies | Yes |
| `beacon milestone graph` | Display dependency graph (waves) | Yes |
| `beacon milestone workspace <id> [--executor ai\|human]` | Create git worktree for isolated milestone development | - |
| `beacon milestone workspace-cleanup <id>` | Remove worktree after work completes | - |

### Task Subcommands

| Command | Description | --json |
|---------|-------------|--------|
| `beacon task add "desc" [-m ms-id] [-t type] [-d detail]` | Add an entry | - |
| `beacon task done <entry-id> [-p progress]` | Mark entry as done | - |
| `beacon task list [-m ms-id]` | List entries | Yes |
| `beacon task show <entry-id>` | Show entry details | Yes |
| `beacon task detail <entry-id> [text]` | View/update detail | - |
| `beacon task update <id> [opts]` | Update fields | Yes |
| `beacon task delete <id>` | Logical delete (cancelled) | Yes |

### Document Subcommands

| Command | Description | --json |
|---------|-------------|--------|
| `beacon doc add "title" [--scope scope] [--ms ms-id] [--op op-id] [--id slug] [--content text]` | Add a document | Yes |
| `beacon doc list [--scope scope] [--ms ms-id] [--op op-id]` | List documents | Yes |
| `beacon doc show <doc-id>` | Show document content | Yes |
| `beacon doc update <doc-id> --content "text"` | Update document content | Yes |

Document scopes: `core` (design principles, always loaded at session start), `spec` (technical specifications), `memo` (investigation notes, volatile).

Use `--ms <ms-id>` to associate a document with a milestone, or `--op <op-id>` to associate with an Operation (e.g. log fetch instructions for `/beacon-operation-review`).

Content can be piped via stdin: `echo 'content' | beacon doc add "title" --scope spec --stdin`

### Save (Non-commit actions)

| Command | Description | --json |
|---------|-------------|--------|
| `beacon save "desc" -m ms-id [--source src]` | Record a non-commit action | Yes |
| `beacon save "desc" -m ms-id --hash <hash>` | Link save to a related commit | Yes |
| `beacon save "desc" --source google_docs --url "..."` | Record with external resource | Yes |

The `save` type records non-git actions (document creation, data analysis, research, etc.) as milestone evidence. When `--hash` is provided, the save entry is linked to a related commit, enabling multi-milestone tracking from a single commit.

Duplicate detection: `source` + (`url` or `revision_id`). `source=manual` skips duplicate checking.

### Logging & Sync

| Command | Description | --json |
|---------|-------------|--------|
| `beacon log [message] [-m ms-id] [-p progress]` | Record HEAD commit | Yes |
| `beacon log --prepare` | Output evaluation context as JSON (read-only) | Yes |
| `beacon log --finalize [--progress N]` | Write evaluation results (`--summary` retired in e-1040) | Yes |
| `beacon sync` | Auto-sync recent git commits to active milestone | - |
| `beacon summary` | View project summary (read-only; writes retired in e-1040, use `project-vision` doc + `beacon session log`) | Yes |

### Pull Requests

| Command | Description | --json |
|---------|-------------|--------|
| `beacon pr create [-m ms-id] [--intent "text"] [gh flags...]` | Run `gh pr create` and auto-record the PR | - |
| `beacon pr add <github-url> [-m ms-id] [--intent "text"]` | Register an existing PR | Yes |
| `beacon pr approve <entry-id> [--rationale "text"]` | Approve a PR (rationale required) | Yes |
| `beacon pr request-changes <entry-id> [--rationale "text"]` | Request changes | Yes |
| `beacon pr reject <entry-id> [--rationale "text"]` | Reject a PR | Yes |
| `beacon pr merge <entry-id>` | Mark as merged | Yes |
| `beacon pr close <entry-id>` | Close without merging | Yes |

Use `/review` Claude Code Skill (not `beacon pr review`) for AI-assisted code review.

### Deploy

| Command | Description | --json |
|---------|-------------|--------|
| `beacon deploy record [--revision <rev>] [--semver <v>] [--desc "text"]` | Record a deployment (auto major/minor) | Yes |
| `beacon deploy record --prepare` | Output deploy context as JSON (read-only) | Yes |
| `beacon deploy record --finalize --desc "text" [--semver v]` | Write AI-generated deploy description | Yes |
| `beacon deploy list` | List deployment history | Yes |

**Major vs minor** is determined automatically from commit history:
- **Major**: one or more milestones newly completed since last deploy
- **Minor**: commits that patch already-shipped milestones (no new completions)

### Code Releases (Push Log)

| Command | Description | --json |
|---------|-------------|--------|
| `beacon push record [--desc "text"]` | Record a git push (auto-collects commits since last push) | - |
| `beacon push record --prepare` | Output push context as JSON (read-only) | Yes |
| `beacon push list` | List push history | Yes |

The `/beacon-push` Skill wraps `beacon push record --prepare` → AI description generation → `beacon push record --desc "..."` for value-based descriptions (same pattern as deploy).

### GitHub Issues

| Command | Description | --json |
|---------|-------------|--------|
| `beacon issue import <number> [-m ms-id]` | Import a GitHub Issue as a beacon task | - |
| `beacon issue sync [-m ms-id]` | Bulk import all open Issues not yet imported | - |
| `beacon issue list` | List open Issues not yet imported | Yes |

Requires `gh` CLI authenticated. When a linked task is marked done, beacon suggests `gh issue close`.

### Operations

Operations track recurring operational workloads (daily batches, incident management) — the maintenance layer alongside development Milestones.

| Command | Description | --json |
|---------|-------------|--------|
| `beacon operation open "title" [--schedule daily\|weekdays\|weekly] [--log-source name]` | Start a new Operation cycle | - |
| `beacon operation close <op-id>` | Close an Operation cycle | - |
| `beacon operation list` | List Operations | Yes |
| `beacon operation show <op-id>` | Show Operation with entries | Yes |
| `beacon run record -o <op-id> --batch <name> --status ok\|warning\|error --desc "..."` | Record a batch run result | - |
| `beacon run list -o <op-id>` | List run records | Yes |
| `beacon incident open "title" -o <op-id> [--desc "..."]` | Open an incident | - |
| `beacon incident close <id> --resolution "..."` | Resolve an incident | - |
| `beacon incident escalate <id> -m <ms-id>` | Escalate incident to a Milestone task | - |

The `/beacon-operation-setup` Skill handles conversational setup and auto-generates a SPEC document for log fetch instructions. The `/beacon-operation-review` Skill fetches logs per SPEC, interprets them, and records the result — triggered by `operation_check_<op-id>` at session start.

### Session Notes

Ephemeral memos that survive context compaction within a session — cleared at session end.

| Command | Description | --json |
|---------|-------------|--------|
| `beacon note "text" [--context "label"]` | Add a session note | - |
| `beacon note list` | Show session notes | Yes |
| `beacon note clear` | Clear all session notes (moved to .bak) | - |

Storage: `.beacon/session_notes.jsonl` (local only, not cloud-synced). Say "remember this" and Claude calls `/beacon-note` automatically. At session end, `/beacon-session-end` prompts to promote important notes to Documents before clearing.

### Retrospectives

| Command | Description | --json |
|---------|-------------|--------|
| `beacon retro [--since DATE] [--until DATE]` | Generate weekly retro data | - |
| `beacon retro done` | Mark current retro as reviewed | - |

### Triggers

| Command | Description | --json |
|---------|-------------|--------|
| `beacon trigger fire <name> [message]` | Fire a trigger (used by dashboard) | - |
| `beacon trigger check` | Check pending triggers | Yes |
| `beacon trigger clear <name>` | Clear a specific trigger | - |

### Cloud & Auth

| Command | Description |
|---------|-------------|
| `beacon auth login` | Sign in with Google |
| `beacon auth logout` | Remove cached credentials |
| `beacon auth status` | Show login status |
| `beacon cloud push` | Upload project to cloud (auto-switches to cloud mode) |
| `beacon cloud pull` | Download project from cloud |
| `beacon cloud list` | List cloud projects |
| `beacon cloud [project-id]` | Open a cloud project (interactive select or by ID) |
| `beacon cloud status` | Show cloud config |
| `beacon cloud off` | Switch back to local mode |

### Other Commands

| Command | Description | --json |
|---------|-------------|--------|
| `beacon` | Launch tmux dashboard + shell | - |
| `beacon init [--name n] [--objective o] [--retro-day d] [--storage local\|cloud]` | Initialize `.beacon/` (flags enable non-interactive use) | - |
| `beacon status` | Show project status | Yes |
| `beacon doctor` | Health check: PATH, hooks, Skills, auth token, cloud config | - |
| `beacon reset` | Move `.beacon/` to a timestamped backup (local only, cloud untouched) | - |
| `beacon search <query> [-m ms-id]` | Full-text search across milestones, entries, PRs | Yes |
| `beacon entry move <entry-id> -t <task-id>` | Move entry under a task | - |
| `beacon help` | Show help | - |
| `beacon --version` | Show version | - |

### Common Options

| Option | Short | Description |
|--------|-------|-------------|
| `--json` | - | Output in JSON format |
| `--ms <id>` | `-m` | Target milestone |
| `--progress <N>` | `-p` | Progress percentage (0-100) |
| `--type <type>` | `-t` | Entry type |
| `--detail <text>` | `-d` | Detail text |
| `--task <id>` | `-t` | Target task ID (entry move) |
| `--all` | `-a` | Show all including cancelled |

## Data Model

### Project State (.beacon/project.json)

```json
{
  "name": "Project name",
  "objective": "High-level goal",
  "summary": "Current context (background, decisions, direction)",
  "milestones": [
    {
      "id": "ms-1",
      "title": "Milestone title",
      "description": "Optional description",
      "status": "todo | in_progress | done | observing | cancelled",
      "progress": 0,
      "target_date": "YYYY-MM-DD | null",
      "depends_on": ["ms-2", "ms-3"],
      "workspace": "optional workspace identifier",
      "workspace_branch": "ms-5-workspace | null",
      "workspace_path": "/path/to/worktree | null",
      "workspace_executor": "ai | human | null",
      "entries": [
        {
          "id": "e-1",
          "type": "commit | task | save | note | pr",
          "description": "Entry description",
          "date": "YYYY-MM-DDThh:mm:ssZ",
          "created_at": "YYYY-MM-DDThh:mm:ssZ",
          "done_at": "YYYY-MM-DDThh:mm:ssZ | null",
          "status": "todo | in_progress | in_review | waiting | done | cancelled",
          "detail": "Detail text (optional)",
          "meta": {
            "hash": "(commit/save) 7-char short hash",
            "message": "(commit) Commit message",
            "source": "(save) manual | google_docs | notion | ...",
            "url": "(save/pr) External resource URL",
            "revision_id": "(save, optional) External system identifier",
            "pr_number": "(pr) GitHub PR number",
            "author": "(pr) GitHub username",
            "pr_status": "(pr) in_review | approved | merged | closed",
            "review_status": "(pr) pending | changes_requested | approved | rejected",
            "intent": "(pr) Why was this PR created?",
            "review_rationale": "(pr) Rationale for approve/reject decision"
          },
          "entries": [
            "(Nested child entries, e.g., commits under a task or PR)"
          ]
        }
      ]
    }
  ],
  "deployments": [
    {
      "id": "deploy-20260517-1",
      "type": "major | minor",
      "date": "2026-05-17T12:00:00Z",
      "environment": "prod",
      "git_hash": "abc1234",
      "commit_hashes": ["abc1234", "def5678"],
      "description": "AI-generated deploy description",
      "newly_completed_ms": ["ms-5"],
      "patch_ms": [],
      "milestones": ["ms-5"],
      "milestone_commits": {"ms-5": ["abc1234"]},
      "linked_release": "release-20260517-1 | null",
      "unassigned_commits": []
    }
  ],
  "releases": [
    {
      "id": "release-20260517-1",
      "date": "2026-05-17",
      "milestones": ["ms-5"],
      "semver": "v1.2.0 | null",
      "description": "Release description",
      "deploy_ids": ["deploy-20260517-1"]
    }
  ],
  "pushes": [
    {
      "id": "push-20260517-1",
      "branch": "main",
      "from_hash": "abc1234",
      "to_hash": "def5678",
      "commit_count": 5,
      "commits": [{"hash": "def5678", "message": "feat: ..."}],
      "summary": "AI-generated push description",
      "pushed_by": "r_kida2",
      "pushed_at": "2026-05-17T12:00:00Z",
      "ms_id": "ms-5"
    }
  ]
}
```

### Documents (.beacon/documents/)

Documents are Markdown files with YAML frontmatter:

```yaml
---
scope: core
---
# Document title

Content in Markdown.
```

In cloud mode, documents are stored via the API and synced on push/pull.

### Cloud Config (.beacon/cloud.json)

```json
{
  "project_id": "project-slug-abc123",
  "api_url": "https://beacon-ai.dev"
}
```

### Mode Config (.beacon/config.json)

```json
{
  "mode": "cloud"
}
```

When `mode` is `cloud`, all CLI commands route through the cloud API. When `local` (default), they read/write `.beacon/project.json` directly.

### Directory Structure

```
.beacon/
  project.json    # Project state (milestones, entries, summary)
  config.json     # Mode config (local/cloud)
  cloud.json      # Cloud project binding (project_id, api_url)
  documents/      # Project documents (Markdown with frontmatter)
  retro/          # Weekly retrospective documents
  triggers/       # Async message queue (dashboard <-> Claude Code)
```

### ID Naming Convention

| Target | Format | Examples |
|--------|--------|----------|
| Milestone | `ms-{N}` | ms-1, ms-2, ms-8 |
| Entry | `e-{N}` | e-1, e-22, e-39 |

IDs are globally unique within a project. Milestone IDs increment from the current max, entry IDs increment across all milestones.

### Status Lifecycle

Milestone:
```
todo → in_progress → done
     ↘ observing     ↗
                   ↘ cancelled (logical delete)
```

Entry:
```
todo → in_progress → done
                   ↘ cancelled (logical delete)
```

Additional entry statuses: `in_review`, `waiting` (for workflow tracking).

Cancelled entries/milestones are hidden from `list` by default. Use `--all` to include them.

### Summary Guidelines

The `summary` field should NOT contain information derivable from the task list (progress %, active milestone name, etc.).

It should contain:
- Why the current tasks are being worked on
- How the project arrived at this point
- Background and decisions the next session needs to know

## Skills (Claude Code Integration)

### Overview

Beacon Skills are the interface for Claude Code to operate beacon. They are installed globally (`~/.claude/skills/`) and triggered by the presence of `.beacon/project.json`.

### Tool-first, AI-generation-embedded Architecture

```
Traditional: Skill prompt → AI decides → calls CLI (fragile, drifts)
Beacon:      CLI controls workflow → specific steps request AI generation → writes result
```

**Two-phase invocation**:
1. `beacon log --prepare` (or `beacon deploy record --prepare`): Outputs context as JSON. No writes.
2. The Skill prompts Claude with a fixed template to generate a progress evaluation or deploy description.
3. `beacon log --finalize --progress N --summary "text"` (or `beacon deploy record --finalize --desc "text"`): Writes the generated result to project.json.

This structurally eliminates the problem of AI ignoring CLAUDE.md prompt instructions.

### Skill List

| Skill | Trigger | Responsibility | Writes |
|-------|---------|----------------|--------|
| `beacon-session-start` | Session start, `/beacon-start` | Load and present current state | No (read-only) |
| `beacon-log` | PostToolUse hook (auto on commit), `/beacon-log` | Record commit + evaluate progress + update summary | Yes (via finalize) |
| `beacon-task` | `/beacon-task` | Task CRUD (add/done/update/delete) | Yes |
| `beacon-session-end` | User end-of-session cues, Claude self-proposal, `/beacon-end` | Update summary + organize open tasks | Yes |
| `beacon-deploy` | PostToolUse hook (auto on deploy), `/beacon-deploy` | Record deployment with AI-generated description | Yes (via finalize) |
| `beacon-push` | PostToolUse hook (auto on git push), `/beacon-push` | Record git push with AI-generated value description | Yes |
| `beacon-retro` | `/beacon-retro`, weekly trigger | Generate and discuss weekly retrospective | Yes |
| `beacon-dispatch` | `/beacon-dispatch`, user requests parallel work | Identify executable milestones, launch parallel sub-agents | No (orchestration only) |
| `beacon-init` | `/beacon-init` | Conversational project init; proposes MS via Project Archaeology for existing repos | Yes |
| `beacon-note` | "remember this", `/beacon-note` | Record an ephemeral session memo (compaction safety) | Yes |
| `beacon-operation-setup` | `/beacon-operation-setup` | Conversational Operation setup with auto-generated SPEC doc | Yes |
| `beacon-operation-review` | `/beacon-operation-review`, operation_check trigger | Fetch logs per SPEC, interpret, record run result | Yes |

### Skill Constraints

- Data must be fetched via `beacon` CLI `--json` output. Never read `.beacon/project.json` directly with a file read tool.
- This ensures Skills remain unchanged if the data layer is replaced with a backend API.

## Dashboard (lib/dashboard.py)

- Runs in the left tmux pane, always visible
- **Local mode**: polls `project.json` file hash for changes
- **Cloud mode**: receives push updates via WebSocket (falls back to throttled HTTP polling)
- Auto-redraws on change detection
- Three view modes: Project (default), Retro, Documents

### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `j` / `↓` | Move down / scroll |
| `k` / `↑` | Move up / scroll |
| `Enter` / `Space` | Expand/collapse (project view) / select document (documents view) |
| `d` | Toggle done entries (project view) |
| `s` | Toggle summary expand/collapse (project view) |
| `D` | Switch to/from documents view |
| `r` | Switch to/from retro view |
| `h` / `ESC` / `←` | Go back (documents detail → list) |
| `q` | Quit (closes tmux session) |

## tmux Session Layout

```
+------------------+----------------------------------------+
| Dashboard (33%)  |  Working Shell (67%)                   |
| (dashboard.py)   |  Run `claude` here                     |
+------------------+----------------------------------------+
```

Session name: `beacon-<first 8 chars of directory path hash>`

## Multi-user & Cloud

### Roles

| Role | Read | Write | Manage members |
|------|------|-------|----------------|
| owner | Yes | Yes | Yes |
| editor | Yes | Yes | No |
| viewer | Yes | No | No |

### Authentication

- Google OAuth via `beacon auth login`
- Credentials stored at `~/.beacon/credentials.json`
- Token auto-refreshes on each API request

### Cloud API

- FastAPI server deployed on Cloud Run
- Firestore for project data persistence
- WebSocket endpoint for real-time dashboard updates
- Role-based authorization on all write endpoints
