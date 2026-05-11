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
lib/dashboard.py                      - Real-time tmux dashboard (Python/curses)
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
│  .beacon/project.json (data layer)      │
│  Replaceable with a backend API         │
└─────────────────────────────────────────┘
```

**Principle**: Skills only bridge `--prepare` output to `--finalize` input. Business logic lives in the CLI.

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
| `beacon milestone add "title" [-d date]` | Add a milestone | - |
| `beacon milestone list` | List milestones | - |
| `beacon milestone start <id>` | Set milestone as active | - |
| `beacon milestone done <id>` | Set milestone as done | - |
| `beacon milestone close <id>` | Close milestone (keeps progress) | - |
| `beacon milestone observe <id>` | Set milestone to observing | - |
| `beacon milestone show <id>` | Show milestone details | Yes |
| `beacon milestone update <id> [opts]` | Update fields | Yes |
| `beacon milestone delete <id>` | Logical delete (cancelled) | Yes |

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

### Other Commands

| Command | Description | --json |
|---------|-------------|--------|
| `beacon` | Launch tmux dashboard + shell | - |
| `beacon init` | Initialize `.beacon/` in current directory | - |
| `beacon status` | Show project status | Yes |
| `beacon log [message] [-m ms-id] [-p progress]` | Record HEAD commit | Yes |
| `beacon log --prepare` | Output evaluation context as JSON (read-only) | Yes |
| `beacon log --finalize [--progress N] [--summary text]` | Write evaluation results | Yes |
| `beacon sync` | Auto-sync recent git commits to active milestone | - |
| `beacon summary [text]` | View/update project summary | Yes |
| `beacon entry move <entry-id> -t <task-id>` | Move entry under a task | - |
| `beacon retro [--since DATE] [--until DATE]` | Generate weekly retro data | - |
| `beacon retro done` | Mark current retro as reviewed | - |

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

## Data Model (.beacon/project.json)

```json
{
  "name": "Project name",
  "objective": "High-level goal",
  "summary": "Current context (background, decisions, direction)",
  "milestones": [
    {
      "id": "ms-1",
      "title": "Milestone title",
      "status": "todo | in_progress | in_review | waiting | done | observing | cancelled",
      "progress": 0,
      "target_date": "YYYY-MM-DD | null",
      "entries": [
        {
          "id": "e-1",
          "type": "commit | task | note",
          "description": "Entry description",
          "date": "YYYY-MM-DD",
          "created_at": "YYYY-MM-DD",
          "done_at": "YYYY-MM-DD | null",
          "status": "todo | in_progress | done | cancelled",
          "detail": "Detail text (optional)",
          "meta": {
            "hash": "(for commits) 7-char short hash",
            "message": "(for commits) Commit message"
          },
          "entries": [
            "(Nested child entries, e.g., commits under a task)"
          ]
        }
      ]
    }
  ]
}
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
1. `beacon log --prepare`: Outputs milestone state, task completion rates, and recent entries as JSON. No writes.
2. The Skill prompts Claude with a fixed template to generate progress evaluation and summary.
3. `beacon log --finalize --progress N --summary "text"`: Writes the generated result to project.json.

This structurally eliminates the problem of AI ignoring CLAUDE.md prompt instructions.

### Skill List

| Skill | Trigger | Responsibility | Writes |
|-------|---------|----------------|--------|
| `beacon-session-start` | Session start, `/beacon-start` | Load and present current state | No (read-only) |
| `beacon-log` | `/beacon-log` | Record commit + evaluate progress + update summary | Yes (via finalize) |
| `beacon-task` | `/beacon-task` | Task CRUD (add/done/update/delete) | Yes |
| `beacon-session-end` | `/beacon-end` | Update summary + organize open tasks | Yes |

### Skill Constraints

- Data must be fetched via `beacon` CLI `--json` output. Never read `.beacon/project.json` directly with a file read tool.
- This ensures Skills remain unchanged if the data layer is replaced with a backend API.

## Dashboard (lib/dashboard.py)

- Runs in the left tmux pane, always visible
- Polls `project.json` file hash every 2 seconds
- Auto-redraws on change detection
- Tree-style display of milestones and entries
- Keyboard: j/k or arrows to navigate, Enter to expand/collapse, d to toggle done, q to quit

## tmux Session Layout

```
+------------------+----------------------------------------+
| Dashboard (33%)  |  Working Shell (67%)                   |
| (dashboard.py)   |  Run `claude` here                     |
+------------------+----------------------------------------+
```

Session name: `beacon-<first 8 chars of directory path hash>`

## Future Plans

### Multi-user Support (ms-6)

- Per-milestone ownership
- PR-driven: Map PR lifecycle (create → in_review → merge → done) to entry status
- Data partitioning: Independent files/API resources per milestone
- Backend: Replace project.json with an API; CLI abstracts the data access layer
