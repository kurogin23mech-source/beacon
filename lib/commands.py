#!/usr/bin/env python3
"""Beacon CLI commands - thin adapter over core.py logic."""

__version__ = "0.1.0"

import json
import os
import re
import shutil
import subprocess
import sys

from store import get_store
import core

# ---------------------------------------------------------------------------
# Store helpers
# ---------------------------------------------------------------------------

def get_project_file():
    return os.environ.get("BEACON_PROJECT_FILE", ".beacon/project.json")


def load_project():
    store = get_store()
    data = store.load_project()
    core.validate_project(data)
    return data


def save_project(data):
    core.validate_project(data)
    store = get_store()
    store.save_project(data)


# ---------------------------------------------------------------------------
# Init (CLI-specific: file creation, hook installation)
# ---------------------------------------------------------------------------

CLAUDE_MD_BEACON_SECTION = """\

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
"""


def _append_claude_md():
    claude_md = "CLAUDE.md"
    marker = "## Beacon Project Management"
    if os.path.exists(claude_md):
        with open(claude_md, "r", encoding="utf-8") as f:
            content = f.read()
        if marker in content:
            # Replace existing section with latest content
            idx = content.index(marker)
            prefix = content[:idx].rstrip('\n') + '\n\n'
            new_section = CLAUDE_MD_BEACON_SECTION.lstrip('\n')
            after = content[idx + len(marker):]
            next_h2 = after.find('\n## ')
            if next_h2 == -1:
                new_content = prefix + new_section
            else:
                new_content = prefix + new_section + after[next_h2:]
            if new_content != content:
                with open(claude_md, "w", encoding="utf-8") as f:
                    f.write(new_content)
                print(f"Updated {claude_md} with beacon rules")
            return
    with open(claude_md, "a", encoding="utf-8") as f:
        f.write(CLAUDE_MD_BEACON_SECTION)
    print(f"Updated {claude_md} with beacon rules")


POST_COMMIT_HOOK = """\
#!/usr/bin/env bash
# Beacon: auto-log commits to the active milestone
# Skip in Claude Code — AI handles logging with milestone/task judgment
if [ -n "$BEACON_CLAUDE_CODE" ]; then
    exit 0
fi
if [ -f ".beacon/project.json" ] && command -v beacon &>/dev/null; then
    beacon log 2>/dev/null || true
fi
"""

BEACON_HOOK_MARKER = "# Beacon: auto-log commits"


def _install_git_hook():
    hook_dir = os.path.join(".git", "hooks")
    if not os.path.isdir(hook_dir):
        return
    hook_path = os.path.join(hook_dir, "post-commit")
    if os.path.exists(hook_path):
        with open(hook_path, "r", encoding="utf-8") as f:
            content = f.read()
        if BEACON_HOOK_MARKER in content:
            return
        with open(hook_path, "a", encoding="utf-8") as f:
            f.write("\n" + POST_COMMIT_HOOK)
    else:
        with open(hook_path, "w", encoding="utf-8") as f:
            f.write(POST_COMMIT_HOOK)
    os.chmod(hook_path, 0o755)
    print("Installed git post-commit hook")


CLAUDE_HOOK_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "bin", "beacon-post-commit-hook.sh"
)

CLAUDE_SAVE_HOOK_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "bin", "beacon-save-hook.sh"
)


def _install_claude_hook():
    settings_path = os.path.expanduser("~/.claude/settings.json")
    settings_dir = os.path.dirname(settings_path)
    os.makedirs(settings_dir, exist_ok=True)
    settings = {}
    if os.path.exists(settings_path):
        with open(settings_path, "r", encoding="utf-8") as f:
            settings = json.load(f)
    hooks = settings.setdefault("hooks", {})
    post_tool_use = hooks.setdefault("PostToolUse", [])

    # Install commit detection hook (matcher: Bash)
    commit_hook_exists = False
    for entry in post_tool_use:
        if entry.get("matcher") == "Bash":
            for h in entry.get("hooks", []):
                cmd = h.get("command", "")
                if "beacon-post-commit-hook" in cmd or "beacon log --prepare" in cmd:
                    commit_hook_exists = True
                    break
    if not commit_hook_exists:
        post_tool_use.append({
            "matcher": "Bash",
            "hooks": [{
                "type": "command",
                "command": CLAUDE_HOOK_SCRIPT,
                "timeout": 10,
                "statusMessage": "Beacon: checking commit...",
            }],
        })

    # Install MCP save hook (matcher: mcp__)
    save_hook_exists = False
    for entry in post_tool_use:
        if entry.get("matcher") == "mcp__":
            for h in entry.get("hooks", []):
                cmd = h.get("command", "")
                if "beacon-save-hook" in cmd:
                    save_hook_exists = True
                    break
    if not save_hook_exists:
        post_tool_use.append({
            "matcher": "mcp__",
            "hooks": [{
                "type": "command",
                "command": CLAUDE_SAVE_HOOK_SCRIPT,
                "timeout": 10,
                "statusMessage": "Beacon: checking MCP operation...",
            }],
        })

    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print("Installed Claude Code PostToolUse hooks")


def _install_skills():
    """Copy beacon skills to ~/.claude/skills/ for Claude Code integration."""
    skills_src = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "skills",
    )
    if not os.path.isdir(skills_src):
        return
    skills_dst = os.path.expanduser("~/.claude/skills")
    os.makedirs(skills_dst, exist_ok=True)
    installed = []
    for fname in os.listdir(skills_src):
        if not fname.endswith(".md"):
            continue
        skill_name = fname[:-3]  # beacon-log.md -> beacon-log
        dst_dir = os.path.join(skills_dst, skill_name)
        os.makedirs(dst_dir, exist_ok=True)
        shutil.copy2(os.path.join(skills_src, fname), os.path.join(dst_dir, "SKILL.md"))
        installed.append(skill_name)
    if installed:
        print(f"Installed skills: {', '.join(sorted(installed))}")


def cmd_init():
    name = os.environ.get("BEACON_NAME", "")
    objective = os.environ.get("BEACON_OBJECTIVE", "")
    pf = get_project_file()
    os.makedirs(os.path.dirname(pf), exist_ok=True)
    retro_day = os.environ.get("BEACON_RETRO_DAY", "monday")
    data = {"name": name, "objective": objective, "milestones": [], "retro_day": retro_day}
    with open(pf, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"Created {pf}")
    print("Next: beacon milestone add")


def cmd_common_setup():
    """Install Claude Code hooks, skills, and CLAUDE.md beacon section (idempotent)."""
    _append_claude_md()
    _install_git_hook()
    _install_claude_hook()
    _install_skills()
    print("Claude Code integration ready.")


def cmd_auth_check():
    """Exit 0 if authenticated, exit 1 if not."""
    from auth import load_credentials
    creds = load_credentials()
    if creds is None:
        print("not_authenticated")
        sys.exit(1)
    email = getattr(creds, "email", "") or ""
    print(f"authenticated:{email}")
    sys.exit(0)


def cmd_cloud_join():
    """Join an existing cloud project (no .beacon/ required)."""
    from auth import load_credentials
    creds = load_credentials()
    if creds is None:
        print("Not logged in. Run: beacon auth login")
        sys.exit(1)

    project_id = os.environ.get("BEACON_CLOUD_PROJECT_ID", "")
    if not project_id:
        print("Error: project ID required")
        sys.exit(1)

    api_url = os.environ.get("BEACON_API_URL", DEFAULT_API_URL)
    token = creds.id_token or creds.token or ""

    from api_client import ApiClient
    client = ApiClient(api_url, token)

    try:
        data = client.get_project(project_id)
    except RuntimeError as e:
        if "404" in str(e):
            print(f"Project '{project_id}' not found in cloud.")
        else:
            print(f"Error: {e}")
        sys.exit(1)

    core.validate_project(data)

    beacon_dir = os.path.dirname(get_project_file()) or ".beacon"
    os.makedirs(beacon_dir, exist_ok=True)

    cloud_config_path = os.path.join(beacon_dir, "cloud.json")
    with open(cloud_config_path, "w", encoding="utf-8") as f:
        json.dump({"project_id": project_id, "api_url": api_url}, f, indent=2, ensure_ascii=False)
        f.write("\n")

    mode_config_path = os.path.join(beacon_dir, "config.json")
    with open(mode_config_path, "w", encoding="utf-8") as f:
        json.dump({"mode": "cloud"}, f, indent=2, ensure_ascii=False)
        f.write("\n")

    # Write project.json directly (LocalStore.save_project requires the file to exist)
    pf = get_project_file()
    with open(pf, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"Joined cloud project: {project_id}")
    print(f"Project: {data.get('name', 'unnamed')}")


# ---------------------------------------------------------------------------
# Milestone commands
# ---------------------------------------------------------------------------

def cmd_milestone_add():
    title = os.environ.get("BEACON_TITLE", "")
    target_date = os.environ.get("BEACON_TARGET_DATE", "")
    description = os.environ.get("BEACON_DESCRIPTION", "")
    data = load_project()
    ms_id = core.milestone_add(data, title, target_date, description=description)
    save_project(data)
    print(f"Added milestone {ms_id}: {title}")


def cmd_milestone_list():
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    show_all = os.environ.get("BEACON_ALL", "") == "1"
    ms_filter_str = os.environ.get("BEACON_MS_FILTER", "")
    data = load_project()

    milestones = data["milestones"]
    if not show_all:
        milestones = [ms for ms in milestones if ms.get("status") != "cancelled"]

    if ms_filter_str:
        ms_ids = [m.strip() for m in ms_filter_str.split(",") if m.strip()]
        all_ids = {ms["id"] for ms in data["milestones"]}
        for ms_id in ms_ids:
            if ms_id not in all_ids:
                print(f"Error: milestone not found: {ms_id}", file=sys.stderr)
                sys.exit(1)
        milestones = [ms for ms in milestones if ms["id"] in ms_ids]

    if json_mode:
        output = {
            "name": data.get("name", ""),
            "summary": data.get("summary", ""),
            "milestones": [],
        }
        for ms in milestones:
            entries = ms.get("entries", [])
            total_tasks, done_tasks = core.count_task_status(entries)
            output["milestones"].append({
                "id": ms["id"],
                "title": ms.get("title", ""),
                "status": ms.get("status", "todo"),
                "progress": ms.get("progress", 0),
                "target_date": ms.get("target_date", ""),
                "total_tasks": total_tasks,
                "done_tasks": done_tasks,
            })
        print(json.dumps(output, ensure_ascii=False))
        return

    icons = {"done": "\u25cf", "in_progress": "\u25d1", "todo": "\u25cb",
             "waiting": "\u25cc", "in_review": "\u25d5", "observing": "\u25d5",
             "cancelled": "\u2718"}
    for ms in milestones:
        icon = icons.get(ms["status"], "?")
        active = " \u25c0 ACTIVE" if ms["status"] == "in_progress" else ""
        progress = ms.get("progress", 0)
        print(f"  {icon} [{ms['id']}] {ms['title']} ({progress}%){active}")


def cmd_milestone_start():
    ms_id = os.environ.get("BEACON_MS_ID", "")
    data = load_project()
    ms = core.milestone_start(data, ms_id)
    save_project(data)
    print(f"Activated: {ms['title']}")


def cmd_milestone_done():
    ms_id = os.environ.get("BEACON_MS_ID", "")
    data = load_project()
    ms = core.milestone_done(data, ms_id)
    save_project(data)
    print(f"Completed: {ms['title']}")


def cmd_milestone_show():
    ms_id = os.environ.get("BEACON_MS_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()

    for ms in data["milestones"]:
        if ms["id"] == ms_id:
            entries = ms.get("entries", [])
            total_tasks, done_tasks = core.count_task_status(entries)
            if json_mode:
                output = {
                    "id": ms["id"],
                    "title": ms.get("title", ""),
                    "description": ms.get("description", ""),
                    "status": ms.get("status", "todo"),
                    "progress": ms.get("progress", 0),
                    "target_date": ms.get("target_date", ""),
                    "total_tasks": total_tasks,
                    "done_tasks": done_tasks,
                    "entries": core.entries_to_json(entries),
                }
                print(json.dumps(output, ensure_ascii=False))
            else:
                icons = {"done": "\u25cf", "in_progress": "\u25d1", "todo": "\u25cb",
                         "waiting": "\u25cc", "in_review": "\u25d5", "cancelled": "\u2718"}
                icon = icons.get(ms["status"], "?")
                print(f"{icon} [{ms['id']}] {ms['title']}")
                if ms.get("description"):
                    print(f"  {ms['description']}")
                print(f"  Status: {ms['status']}  Progress: {ms.get('progress', 0)}%")
                print(f"  Target: {ms.get('target_date') or '-'}")
                print(f"  Tasks: {done_tasks}/{total_tasks} done")
            return

    print(f"Milestone not found: {ms_id}")
    sys.exit(1)


def cmd_milestone_update():
    ms_id = os.environ.get("BEACON_MS_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()
    try:
        ms = core.milestone_update(
            data, ms_id,
            title=os.environ.get("BEACON_TITLE", ""),
            progress=os.environ.get("BEACON_PROGRESS", ""),
            target_date=os.environ.get("BEACON_TARGET_DATE", ""),
            status=os.environ.get("BEACON_STATUS", ""),
            description=os.environ.get("BEACON_DESCRIPTION", ""),
        )
    except ValueError as e:
        print(str(e))
        sys.exit(1)
    save_project(data)
    if json_mode:
        print(json.dumps({"id": ms["id"], "title": ms["title"],
                          "status": ms["status"], "progress": ms.get("progress", 0)},
                         ensure_ascii=False))
    else:
        print(f"Updated: [{ms['id']}] {ms['title']}")


def cmd_milestone_delete():
    ms_id = os.environ.get("BEACON_MS_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()
    try:
        ms = core.milestone_delete(data, ms_id)
    except ValueError as e:
        print(str(e))
        sys.exit(1)
    save_project(data)
    if json_mode:
        print(json.dumps({"id": ms["id"], "status": "cancelled"}, ensure_ascii=False))
    else:
        print(f"Cancelled: [{ms['id']}] {ms['title']}")


# ---------------------------------------------------------------------------
# Log commands
# ---------------------------------------------------------------------------

def cmd_log():
    summary = os.environ.get("BEACON_SUMMARY", "")
    commit_hash = os.environ.get("BEACON_HASH", "")
    message = os.environ.get("BEACON_MESSAGE", "")
    date = os.environ.get("BEACON_DATE", "")
    ms_id = os.environ.get("BEACON_MS_ID", "")
    progress = os.environ.get("BEACON_PROGRESS", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    data = load_project()
    result = core.log_commit(
        data, ms_id=ms_id, commit_hash=commit_hash,
        message=message, date=date, summary=summary, progress=progress,
    )
    save_project(data)

    if json_mode:
        print(json.dumps(result, ensure_ascii=False))
    elif result["status"] == "duplicate":
        print(f"Already logged: {commit_hash}")
    else:
        loc = f"[{result['matched_task']}]" if "matched_task" in result else result["milestone_title"]
        print(f"Logged {commit_hash} to {loc}")
        if progress:
            print(f"  Progress: {result['progress']}%")


def cmd_log_prepare():
    commit_hash = os.environ.get("BEACON_HASH", "")
    message = os.environ.get("BEACON_MESSAGE", "")
    date = os.environ.get("BEACON_DATE", "")
    ms_id = os.environ.get("BEACON_MS_ID", "")
    summary_text = os.environ.get("BEACON_SUMMARY", "")

    data = load_project()

    if ms_id:
        for ms in data["milestones"]:
            if ms["id"] == ms_id:
                targets = [ms]
                break
        else:
            print(f"Milestone not found: {ms_id}")
            sys.exit(1)
    else:
        targets = [ms for ms in data["milestones"] if ms["status"] in ("todo", "in_progress", "observing")]
        if not targets:
            print("No active milestone. Run: beacon milestone start <ms-id>")
            sys.exit(1)

    output = {
        "commit": {"hash": commit_hash, "message": message, "date": date, "summary": summary_text},
        "current_summary": data.get("summary", ""),
    }

    if len(targets) == 1:
        output["milestone"] = core.milestone_prepare_info(targets[0])
    else:
        output["candidates"] = [core.milestone_prepare_info(ms) for ms in targets]

    print(json.dumps(output, ensure_ascii=False))


def cmd_log_finalize():
    commit_hash = os.environ.get("BEACON_HASH", "")
    message = os.environ.get("BEACON_MESSAGE", "")
    date = os.environ.get("BEACON_DATE", "")
    ms_id = os.environ.get("BEACON_MS_ID", "")
    summary_text = os.environ.get("BEACON_SUMMARY", "")
    progress = os.environ.get("BEACON_PROGRESS", "")
    new_summary = os.environ.get("BEACON_NEW_SUMMARY", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    data = load_project()
    result = core.log_commit(
        data, ms_id=ms_id, commit_hash=commit_hash,
        message=message, date=date, summary=summary_text, progress=progress,
    )

    if new_summary:
        data["summary"] = new_summary

    save_project(data)

    if json_mode:
        result["summary_updated"] = bool(new_summary)
        print(json.dumps(result, ensure_ascii=False))
    else:
        if result["status"] == "duplicate":
            print(f"Already logged: {commit_hash} (updated progress/summary)")
        else:
            loc = f"[{result['matched_task']}]" if "matched_task" in result else result["milestone_title"]
            print(f"Logged {commit_hash} → {loc}")
        if progress:
            print(f"  Progress: {result['progress']}%")
        if new_summary:
            print(f"  Summary updated.")


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def cmd_sync():
    ms_id = os.environ.get("BEACON_MS_ID", "")
    data = load_project()
    target = core.find_target_milestone(data, ms_id)

    entries = target.setdefault("entries", [])
    existing_hashes = set()
    for entry in entries:
        if entry.get("type") == "commit":
            h = entry.get("meta", {}).get("hash", "")
            if h:
                existing_hashes.add(h[:7])

    result = subprocess.run(
        ["git", "log", "--oneline", "-20", "--pretty=format:%h|%s|%ci"],
        capture_output=True, text=True,
    )

    added = 0
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        h, msg, date_str = parts
        if h not in existing_hashes:
            commit_date = date_str.split(" ")[0]
            entries.insert(0, {
                "id": core.next_entry_id(data),
                "type": "commit",
                "description": msg,
                "date": commit_date,
                "created_at": commit_date,
                "done_at": commit_date,
                "status": "done",
                "meta": {"hash": h, "message": msg},
            })
            added += 1

    if added:
        save_project(data)
        print(f"Synced {added} new commits to: {target['title']}")
    else:
        print("No new commits to sync.")


# ---------------------------------------------------------------------------
# Task commands
# ---------------------------------------------------------------------------

def cmd_task_add():
    description = os.environ.get("BEACON_DESCRIPTION", "")
    ms_id = os.environ.get("BEACON_MS_ID", "")
    entry_type = os.environ.get("BEACON_TYPE", "task")
    date = os.environ.get("BEACON_DATE", "")
    detail = os.environ.get("BEACON_DETAIL", "")
    requested_by = os.environ.get("BEACON_REQUESTED_BY", "")

    data = load_project()
    target = core.find_target_milestone(data, ms_id)
    eid = core.task_add(data, ms_id, description, entry_type=entry_type,
                        date=date, detail=detail, requested_by=requested_by)
    save_project(data)
    from_str = f" (from {requested_by})" if requested_by else ""
    print(f"Added {entry_type} [{eid}] to {target['title']}: {description}{from_str}")


def cmd_task_done():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    progress = os.environ.get("BEACON_PROGRESS", "")
    import datetime
    today = datetime.date.today().isoformat()

    data = load_project()
    # Check if this is a PR entry — use pr_merge instead
    result = core.find_entry(data, entry_id)
    if result:
        _, _, entry, _ = result
        if entry.get("type") == "pr":
            print(f"Note: {entry_id} is a PR entry. Use 'beacon pr merge {entry_id}' to mark as merged.")
            print(f"      Marking done without updating pr_status.")
    ms, entry = core.task_done(data, entry_id, date=today)
    print(f"Done: [{entry_id}] {entry['description']}")
    core.update_progress(ms, progress)
    if progress:
        print(f"  Progress: {ms.get('progress', 0)}%")
    save_project(data)


def cmd_task_list():
    ms_id = os.environ.get("BEACON_MS_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    show_all = os.environ.get("BEACON_ALL", "") == "1"
    type_filter = os.environ.get("BEACON_TYPE_FILTER", "")
    data = load_project()
    target = core.find_target_milestone(data, ms_id)

    entries = core.filter_cancelled(target.get("entries", []), show_all)

    if type_filter:
        entries = [e for e in entries if e.get("type") == type_filter]

    if json_mode:
        output = {
            "milestone_id": target["id"],
            "milestone_title": target.get("title", ""),
            "entries": core.entries_to_json(entries),
        }
        print(json.dumps(output, ensure_ascii=False))
        return

    if not entries:
        print(f"No entries in {target['title']}")
        return

    icons = {"done": "\u25cf", "todo": "\u25cb", "cancelled": "\u2718"}
    for entry in entries:
        icon = icons.get(entry.get("status", "todo"), "?")
        etype = entry.get("type", "?")
        print(f"  {icon} [{entry['id']}] ({etype}) {entry['description']}")


def cmd_task_show():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()
    result = core.find_entry(data, entry_id)
    if not result:
        print(f"Entry not found: {entry_id}")
        sys.exit(1)
    ms, _, entry, _ = result

    if json_mode:
        output = {"milestone_id": ms["id"], "milestone_title": ms.get("title", "")}
        entry_json = core.entries_to_json([entry])[0]
        output.update(entry_json)
        print(json.dumps(output, ensure_ascii=False))
        return

    icons = {"done": "\u25cf", "todo": "\u25cb", "in_progress": "\u25d1",
             "waiting": "\u25cc", "in_review": "\u25d5", "observing": "\u25d5",
             "cancelled": "\u2718"}
    icon = icons.get(entry.get("status", "todo"), "?")
    print(f"{icon} [{entry['id']}] {entry.get('description', '')}")
    print(f"  Milestone: [{ms['id']}] {ms['title']}")
    print(f"  Type: {entry.get('type', '?')}  Status: {entry.get('status', '?')}")
    print(f"  Created: {entry.get('created_at', '-')}  Done: {entry.get('done_at', '-')}")
    # Show PR-specific fields
    if entry.get("type") == "pr":
        meta = entry.get("meta", {})
        print(f"  PR status: {meta.get('pr_status', '-')}  Review: {meta.get('review_status', '-')}")
        if meta.get("url"):
            print(f"  URL: {meta['url']}")
        if meta.get("intent"):
            print(f"  Intent: {meta['intent']}")
        if meta.get("review_rationale"):
            print(f"  Rationale: {meta['review_rationale']}")
        if meta.get("author"):
            print(f"  Author: {meta['author']}")
    detail = entry.get("detail", "")
    if detail:
        print(f"\n{detail}")


def cmd_task_detail():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    detail = os.environ.get("BEACON_DETAIL", "")
    data = load_project()
    result = core.find_entry(data, entry_id)
    if not result:
        print(f"Entry not found: {entry_id}")
        sys.exit(1)
    _, _, entry, _ = result
    if detail:
        entry["detail"] = detail
        save_project(data)
        print(f"Updated detail for [{entry_id}] {entry.get('description', '')}")
    else:
        print(entry.get("detail", "(no detail)"))


def cmd_task_update():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    import datetime
    today = datetime.date.today().isoformat()

    data = load_project()
    try:
        ms, entry = core.task_update(
            data, entry_id,
            description=os.environ.get("BEACON_DESCRIPTION", ""),
            status=os.environ.get("BEACON_STATUS", ""),
            detail=os.environ.get("BEACON_DETAIL", ""),
            date=today,
        )
    except ValueError as e:
        print(str(e))
        sys.exit(1)
    save_project(data)
    if json_mode:
        print(json.dumps(core.entries_to_json([entry])[0], ensure_ascii=False))
    else:
        print(f"Updated: [{entry_id}] {entry.get('description', '')}")


def cmd_task_delete():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()
    try:
        entry = core.task_delete(data, entry_id)
    except ValueError as e:
        print(str(e))
        sys.exit(1)
    save_project(data)
    if json_mode:
        print(json.dumps({"id": entry_id, "status": "cancelled"}, ensure_ascii=False))
    else:
        print(f"Cancelled: [{entry_id}] {entry.get('description', '')}")


def cmd_entry_move():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    task_id = os.environ.get("BEACON_TASK_ID", "")
    ms_id = os.environ.get("BEACON_MS_ID", "")

    if not entry_id or (not task_id and not ms_id):
        print("Usage: beacon entry move <entry-id> -t <task-id> | -m <ms-id>")
        sys.exit(1)

    data = load_project()
    try:
        core.entry_move(data, entry_id, task_id=task_id, ms_id=ms_id)
    except ValueError as e:
        print(str(e))
        sys.exit(1)
    save_project(data)
    if ms_id:
        print(f"Moved [{entry_id}] to {ms_id}")
    else:
        print(f"Moved [{entry_id}] under [{task_id}]")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def cmd_summary():
    text = os.environ.get("BEACON_SUMMARY_TEXT", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()
    if text:
        data["summary"] = text
        save_project(data)
        print(f"Summary updated.")
    elif json_mode:
        print(json.dumps({"summary": data.get("summary", "")}, ensure_ascii=False))
    else:
        print(data.get("summary", "(未設定)"))


# ---------------------------------------------------------------------------
# Save (ms-16)
# ---------------------------------------------------------------------------

def cmd_save():
    description = os.environ.get("BEACON_DESCRIPTION", "")
    ms_id = os.environ.get("BEACON_MS_ID", "")
    source = os.environ.get("BEACON_SOURCE", "")
    url = os.environ.get("BEACON_URL", "")
    revision_id = os.environ.get("BEACON_REVISION_ID", "")
    hash_val = os.environ.get("BEACON_HASH", "")
    progress = os.environ.get("BEACON_PROGRESS", "")
    date = os.environ.get("BEACON_DATE", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not source:
        print("Error: --source is required", file=sys.stderr)
        sys.exit(1)
    if not description:
        print("Error: description is required", file=sys.stderr)
        sys.exit(1)

    data = load_project()
    result = core.save_entry(data, ms_id=ms_id, description=description,
                             source=source, date=date, url=url,
                             revision_id=revision_id, hash=hash_val,
                             progress=progress)
    save_project(data)

    if json_mode:
        print(json.dumps(result, ensure_ascii=False))
    else:
        if result["status"] == "duplicate":
            print(f"Duplicate save skipped (source={source}, ms={result['milestone']})")
        else:
            print(f"Saved [{result['entry_id']}] to {result['milestone']}: {description}")


# ---------------------------------------------------------------------------
# Milestone depends / workspace / graph (ms-17)
# ---------------------------------------------------------------------------

def cmd_milestone_depends():
    ms_id = os.environ.get("BEACON_MS_ID", "")
    depends_on_str = os.environ.get("BEACON_DEPENDS_ON", "")
    clear = os.environ.get("BEACON_CLEAR", "") == "1"
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not ms_id:
        print("Error: milestone ID is required", file=sys.stderr)
        sys.exit(1)

    data = load_project()
    if clear:
        ms = core.milestone_depends(data, ms_id, [])
    else:
        if not depends_on_str:
            print("Error: --on is required (or use --clear)", file=sys.stderr)
            sys.exit(1)
        deps = [d.strip() for d in depends_on_str.split(",") if d.strip()]
        ms = core.milestone_depends(data, ms_id, deps)
    save_project(data)

    if json_mode:
        print(json.dumps({"id": ms["id"], "depends_on": ms.get("depends_on", [])}, ensure_ascii=False))
    else:
        deps = ms.get("depends_on", [])
        if deps:
            print(f"{ms_id} depends on: {', '.join(deps)}")
        else:
            print(f"{ms_id}: dependencies cleared")


def cmd_milestone_workspace():
    ms_id = os.environ.get("BEACON_MS_ID", "")
    workspace = os.environ.get("BEACON_WORKSPACE", "")
    clear = os.environ.get("BEACON_CLEAR", "") == "1"
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not ms_id:
        print("Error: milestone ID is required", file=sys.stderr)
        sys.exit(1)

    data = load_project()
    if clear:
        ms = core.milestone_workspace(data, ms_id, "")
    else:
        if not workspace:
            print("Error: --dir is required (or use --clear)", file=sys.stderr)
            sys.exit(1)
        ms = core.milestone_workspace(data, ms_id, workspace)
    save_project(data)

    if json_mode:
        print(json.dumps({"id": ms["id"], "workspace": ms.get("workspace")}, ensure_ascii=False))
    else:
        ws = ms.get("workspace")
        if ws:
            print(f"{ms_id} workspace: {ws}")
        else:
            print(f"{ms_id}: workspace cleared")


def cmd_milestone_graph():
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    data = load_project()
    graph = core.milestone_graph(data)

    if json_mode:
        print(json.dumps(graph, ensure_ascii=False))
    else:
        for wave_info in graph["waves"]:
            wave_num = wave_info["wave"]
            cycle_marker = " [CYCLE]" if wave_info.get("cycle") else ""
            ms_ids = wave_info["milestones"]
            # Build display lines
            lines = []
            for ms_id in ms_ids:
                node = next((n for n in graph["nodes"] if n["id"] == ms_id), None)
                if node:
                    deps = node.get("depends_on", [])
                    dep_str = f" <- {', '.join(deps)}" if deps else ""
                    status_icon = {"done": "●", "in_progress": "◐", "todo": "○",
                                   "waiting": "◌", "observing": "◔"}.get(node["status"], "?")
                    lines.append(f"  {status_icon} {ms_id} {node['title']} ({node['progress']}%){dep_str}")
            print(f"Wave {wave_num}{cycle_marker}:")
            for line in lines:
                print(line)


# ---------------------------------------------------------------------------
# Retro
# ---------------------------------------------------------------------------

def cmd_retro_prepare():
    since = os.environ.get("BEACON_SINCE", "")
    until = os.environ.get("BEACON_UNTIL", "")
    data = load_project()

    weekly_milestones = []
    for ms in data.get("milestones", []):
        ms_entries = core.collect_retro_entries(ms.get("entries", []), since, until)
        if ms_entries:
            weekly_milestones.append({
                "id": ms["id"],
                "title": ms.get("title", ""),
                "status": ms.get("status", ""),
                "progress": ms.get("progress", 0),
                "entries": ms_entries,
            })

    output = {
        "project": data.get("name", ""),
        "period": {"since": since, "until": until},
        "summary": data.get("summary", ""),
        "milestones": weekly_milestones,
    }
    print(json.dumps(output, ensure_ascii=False))


def cmd_retro_done():
    import datetime
    today = datetime.date.today()
    year, week, _ = today.isocalendar()
    current_week = f"{year}-W{week:02d}"

    project_dir = os.path.dirname(get_project_file())
    retro_dir = os.path.join(project_dir, "retro")
    os.makedirs(retro_dir, exist_ok=True)
    reviewed_path = os.path.join(retro_dir, ".reviewed")
    with open(reviewed_path, "w") as f:
        f.write(current_week + "\n")

    triggers_dir = os.path.join(project_dir, "triggers")
    retro_trigger = os.path.join(triggers_dir, "retro.json")
    if os.path.exists(retro_trigger):
        os.remove(retro_trigger)

    print(f"Retro reviewed: {current_week}")


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------

def _get_triggers_dir():
    project_dir = os.path.dirname(get_project_file())
    return os.path.join(project_dir, "triggers")


DAY_NAMES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
             "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
             "friday": 4, "saturday": 5, "sunday": 6}


def _get_retro_day():
    try:
        data = load_project()
        day_str = data.get("retro_day", "friday").lower()
        return DAY_NAMES.get(day_str, 4)
    except Exception:
        return 4


def _auto_fire_retro_trigger():
    import datetime
    today = datetime.date.today()
    retro_day = _get_retro_day()
    if today.weekday() != retro_day:
        return
    year, week, _ = today.isocalendar()
    current_week = f"{year}-W{week:02d}"
    project_dir = os.path.dirname(get_project_file())
    reviewed_path = os.path.join(project_dir, "retro", ".reviewed")
    try:
        with open(reviewed_path, "r") as f:
            if f.read().strip() >= current_week:
                return
    except (FileNotFoundError, IOError):
        pass
    triggers_dir = os.path.join(project_dir, "triggers")
    trigger_path = os.path.join(triggers_dir, "retro.json")
    if os.path.exists(trigger_path):
        return
    os.makedirs(triggers_dir, exist_ok=True)
    trigger_data = {
        "name": "retro",
        "message": f"今週の振り返りがまだです（{current_week}）。/beacon-retro で開始しますか？",
        "created_at": today.isoformat(),
    }
    with open(trigger_path, "w") as f:
        json.dump(trigger_data, f, ensure_ascii=False)
        f.write("\n")


def _cleanup_stale_triggers():
    import datetime
    today = datetime.date.today()
    triggers_dir = _get_triggers_dir()
    retro_path = os.path.join(triggers_dir, "retro.json")
    if not os.path.exists(retro_path):
        return
    try:
        with open(retro_path, "r") as f:
            trigger = json.load(f)
        created = datetime.date.fromisoformat(trigger["created_at"][:10])
        if today > created:
            os.remove(retro_path)
    except (json.JSONDecodeError, KeyError, ValueError, IOError):
        pass


def cmd_trigger_fire():
    trigger_name = os.environ.get("BEACON_TRIGGER_NAME", "")
    trigger_message = os.environ.get("BEACON_TRIGGER_MESSAGE", "")
    if not trigger_name:
        print("Error: trigger name required")
        sys.exit(1)
    triggers_dir = _get_triggers_dir()
    os.makedirs(triggers_dir, exist_ok=True)
    trigger_path = os.path.join(triggers_dir, f"{trigger_name}.json")
    if os.path.exists(trigger_path):
        return
    import datetime
    trigger_data = {
        "name": trigger_name,
        "message": trigger_message,
        "created_at": datetime.datetime.now().isoformat(),
    }
    with open(trigger_path, "w") as f:
        json.dump(trigger_data, f, ensure_ascii=False)
        f.write("\n")


def cmd_trigger_check():
    _auto_fire_retro_trigger()
    _cleanup_stale_triggers()
    triggers_dir = _get_triggers_dir()
    if not os.path.isdir(triggers_dir):
        print("[]")
        return
    triggers = []
    for fname in sorted(os.listdir(triggers_dir)):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(triggers_dir, fname)
        try:
            with open(fpath, "r") as f:
                triggers.append(json.load(f))
        except (json.JSONDecodeError, IOError):
            pass
    print(json.dumps(triggers, ensure_ascii=False))


def cmd_trigger_clear():
    trigger_name = os.environ.get("BEACON_TRIGGER_NAME", "")
    if not trigger_name:
        print("Error: trigger name required")
        sys.exit(1)
    triggers_dir = _get_triggers_dir()
    trigger_path = os.path.join(triggers_dir, f"{trigger_name}.json")
    if os.path.exists(trigger_path):
        os.remove(trigger_path)
        print(f"Cleared trigger: {trigger_name}")
    else:
        print(f"No trigger: {trigger_name}")


# ---------------------------------------------------------------------------
# Document commands
# ---------------------------------------------------------------------------

VALID_SCOPES = ("core", "spec", "memo")
DEFAULT_SCOPE = "memo"


def _get_docs_dir():
    project_dir = os.path.dirname(get_project_file()) or ".beacon"
    return os.path.join(project_dir, "documents")


def _doc_slug(title):
    """Generate a file-safe slug from a document title."""
    slug = re.sub(r"[^\w]+", "-", title.lower()).strip("-")
    slug = re.sub(r"-+", "-", slug)
    return slug or "untitled"


def _is_cloud_mode():
    """Check if we're in cloud mode (has cloud.json + config mode=cloud)."""
    beacon_dir = os.path.dirname(get_project_file()) or ".beacon"
    config_path = os.path.join(beacon_dir, "config.json")
    if os.environ.get("BEACON_CLOUD") == "1":
        return True
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        return config.get("mode") == "cloud"
    return False


def _parse_frontmatter(text):
    """Parse YAML-like frontmatter from markdown text.

    Returns (metadata_dict, body_text).
    """
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    header = text[4:end]
    body = text[end + 4:].lstrip("\n")
    meta = {}
    for line in header.split("\n"):
        line = line.strip()
        if ":" in line:
            key, val = line.split(":", 1)
            meta[key.strip()] = val.strip()
    return meta, body


def _add_frontmatter(content, scope, milestone=""):
    """Prepend frontmatter to content, or update existing scope/milestone."""
    meta, body = _parse_frontmatter(content)
    meta["scope"] = scope
    if milestone:
        meta["milestone"] = milestone
    elif "milestone" not in meta:
        pass  # keep existing or absent
    lines = ["---"]
    for k, v in meta.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines) + body


def _read_local_doc(fpath):
    """Read a local document file and return parsed metadata."""
    import datetime
    fname = os.path.basename(fpath)
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    meta, body = _parse_frontmatter(content)
    scope = meta.get("scope", DEFAULT_SCOPE)
    milestone = meta.get("milestone", "")
    # Find title from first heading in body
    first_line = ""
    for line in body.split("\n"):
        line = line.strip()
        if line:
            first_line = line
            break
    title = first_line.lstrip("# ") if first_line.startswith("#") else fname[:-3]
    stat = os.stat(fpath)
    updated = datetime.datetime.fromtimestamp(stat.st_mtime).isoformat()
    result = {
        "doc_id": fname[:-3],
        "title": title,
        "scope": scope,
        "content": content,
        "updated_at": updated,
    }
    if milestone:
        result["milestone"] = milestone
    return result


def cmd_doc_list():
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    scope_filter = os.environ.get("BEACON_SCOPE", "")
    ms_filter = os.environ.get("BEACON_MS", "")

    if _is_cloud_mode():
        client, config = _get_api_client()
        docs = client.list_documents(config["project_id"])
    else:
        docs_dir = _get_docs_dir()
        docs = []
        if os.path.isdir(docs_dir):
            for fname in sorted(os.listdir(docs_dir)):
                if not fname.endswith(".md"):
                    continue
                doc = _read_local_doc(os.path.join(docs_dir, fname))
                entry = {
                    "doc_id": doc["doc_id"],
                    "title": doc["title"],
                    "scope": doc["scope"],
                    "updated_at": doc["updated_at"],
                }
                if doc.get("milestone"):
                    entry["milestone"] = doc["milestone"]
                docs.append(entry)

    if scope_filter:
        docs = [d for d in docs if d.get("scope") == scope_filter]
    if ms_filter:
        docs = [d for d in docs if d.get("milestone") == ms_filter]

    if json_mode:
        print(json.dumps(docs, ensure_ascii=False))
    else:
        if not docs:
            print("No documents.")
            return
        scope_icons = {"core": "*", "spec": "+", "memo": "-"}
        for doc in docs:
            icon = scope_icons.get(doc.get("scope", "memo"), "?")
            print(f"  {icon} [{doc.get('scope', 'memo')}] {doc['doc_id']}: {doc['title']}")


def cmd_doc_show():
    doc_id = os.environ.get("BEACON_DOC_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not doc_id:
        print("Error: doc_id required")
        sys.exit(1)

    if _is_cloud_mode():
        client, config = _get_api_client()
        doc = client.get_document(config["project_id"], doc_id)
    else:
        docs_dir = _get_docs_dir()
        fpath = os.path.join(docs_dir, f"{doc_id}.md")
        if not os.path.exists(fpath):
            print(f"Document not found: {doc_id}")
            sys.exit(1)
        doc = _read_local_doc(fpath)

    if json_mode:
        print(json.dumps(doc, ensure_ascii=False))
    else:
        print(doc.get("content", ""))


def cmd_doc_add():
    title = os.environ.get("BEACON_TITLE", "")
    content = os.environ.get("BEACON_CONTENT", "")
    doc_id = os.environ.get("BEACON_DOC_ID", "")
    scope = os.environ.get("BEACON_SCOPE", DEFAULT_SCOPE)
    milestone = os.environ.get("BEACON_MS", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not title:
        print("Error: title required")
        sys.exit(1)

    if scope not in VALID_SCOPES:
        print(f"Error: scope must be one of {VALID_SCOPES}")
        sys.exit(1)

    # Read content from stdin if not provided via env
    if not content and not sys.stdin.isatty():
        content = sys.stdin.read()

    if not content:
        print("Error: content required (pass via BEACON_CONTENT or stdin)")
        sys.exit(1)

    # Add frontmatter with scope and milestone
    content = _add_frontmatter(content, scope, milestone)

    if _is_cloud_mode():
        client, config = _get_api_client()
        if doc_id:
            result = client.update_document(config["project_id"], doc_id, title, content)
        else:
            result = client.create_document(config["project_id"], title, content)
        doc_id = result["doc_id"]
    else:
        docs_dir = _get_docs_dir()
        os.makedirs(docs_dir, exist_ok=True)
        if not doc_id:
            doc_id = _doc_slug(title)
        fpath = os.path.join(docs_dir, f"{doc_id}.md")
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(content)

    import datetime
    data = load_project()
    today = datetime.date.today().isoformat()
    core.save_entry(data, ms_id=milestone, description=f"doc add: {title} ({scope})",
                    source="auto", date=today, revision_id=doc_id,
                    url=None, hash=None, progress=None)
    save_project(data)

    if json_mode:
        print(json.dumps({"doc_id": doc_id, "title": title, "scope": scope}, ensure_ascii=False))
    else:
        print(f"Saved: {doc_id} [{scope}] ({title})")


def cmd_doc_update():
    doc_id = os.environ.get("BEACON_DOC_ID", "")
    content = os.environ.get("BEACON_CONTENT", "")
    title = os.environ.get("BEACON_TITLE", "")
    scope = os.environ.get("BEACON_SCOPE", "")
    milestone = os.environ.get("BEACON_MS", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not doc_id:
        print("Error: doc_id required")
        sys.exit(1)

    # Read content from stdin if not provided via env
    if not content and not sys.stdin.isatty():
        content = sys.stdin.read()

    # Fetch existing document to merge fields
    if _is_cloud_mode():
        client, config = _get_api_client()
        existing = client.get_document(config["project_id"], doc_id)
    else:
        docs_dir = _get_docs_dir()
        fpath = os.path.join(docs_dir, f"{doc_id}.md")
        if not os.path.exists(fpath):
            print(f"Document not found: {doc_id}")
            sys.exit(1)
        existing = _read_local_doc(fpath)

    # Use existing values as defaults
    if not title:
        title = existing.get("title", "")
    if not scope:
        scope = existing.get("scope", DEFAULT_SCOPE)
    if not milestone:
        milestone = existing.get("milestone", "")
    if not content:
        content = existing.get("content", "")

    # Rebuild with frontmatter
    content = _add_frontmatter(content, scope, milestone)

    if _is_cloud_mode():
        client.update_document(config["project_id"], doc_id, title, content)
    else:
        fpath = os.path.join(docs_dir, f"{doc_id}.md")
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(content)

    import datetime
    data = load_project()
    today = datetime.date.today().isoformat()
    core.save_entry(data, ms_id=milestone, description=f"doc update: {title} ({scope})",
                    source="auto", date=today, revision_id=doc_id,
                    url=None, hash=None, progress=None)
    save_project(data)

    if json_mode:
        print(json.dumps({"doc_id": doc_id, "title": title, "scope": scope}, ensure_ascii=False))
    else:
        print(f"Updated: {doc_id} [{scope}] ({title})")


def cmd_doc_delete():
    doc_id = os.environ.get("BEACON_DOC_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not doc_id:
        print("Error: doc_id required")
        sys.exit(1)

    if _is_cloud_mode():
        client, config = _get_api_client()
        try:
            client.delete_document(config["project_id"], doc_id)
        except Exception as e:
            print(f"Error: {e}")
            sys.exit(1)
    else:
        docs_dir = _get_docs_dir()
        fpath = os.path.join(docs_dir, f"{doc_id}.md")
        if not os.path.exists(fpath):
            print(f"Document not found: {doc_id}")
            sys.exit(1)
        os.remove(fpath)

    if json_mode:
        print(json.dumps({"doc_id": doc_id, "deleted": True}, ensure_ascii=False))
    else:
        print(f"Deleted: {doc_id}")


# ---------------------------------------------------------------------------
# Cloud commands
# ---------------------------------------------------------------------------

DEFAULT_API_URL = "https://beacon-ai.dev"


def _get_cloud_config_path():
    beacon_dir = os.path.dirname(get_project_file()) or ".beacon"
    return os.path.join(beacon_dir, "cloud.json")


def _ensure_cloud_config():
    config_path = _get_cloud_config_path()
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)

    data = load_project()
    name = data.get("name", "project")
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "project"
    import hashlib
    h = hashlib.md5(os.path.abspath(get_project_file()).encode()).hexdigest()[:6]
    project_id = f"{slug}-{h}"

    api_url = os.environ.get("BEACON_API_URL", DEFAULT_API_URL)
    config = {"project_id": project_id, "api_url": api_url}
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"Created {config_path} (project_id: {project_id})")
    return config


def _get_api_client():
    """Create an ApiClient from cloud.json config and auth credentials."""
    from auth import load_credentials
    creds = load_credentials()
    if creds is None:
        print("Not logged in. Run: beacon auth login")
        sys.exit(1)

    config_path = _get_cloud_config_path()
    if not os.path.exists(config_path):
        print("No cloud.json found. Run 'beacon cloud push' first.")
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    api_url = config.get("api_url", DEFAULT_API_URL)
    token = creds.id_token or creds.token or ""

    from api_client import ApiClient
    return ApiClient(api_url, token), config


def cmd_cloud_list():
    """List cloud projects via API."""
    from auth import load_credentials
    creds = load_credentials()
    if creds is None:
        print("Not logged in. Run: beacon auth login")
        sys.exit(1)

    # cloud list may be called before cloud.json exists, so read api_url from env
    api_url = os.environ.get("BEACON_API_URL", DEFAULT_API_URL)
    config_path = _get_cloud_config_path()
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        api_url = config.get("api_url", api_url)

    from api_client import ApiClient
    client = ApiClient(api_url, creds.id_token or creds.token or "")

    try:
        projects = client.list_projects()
    except RuntimeError as e:
        print(f"Error: {e}")
        sys.exit(1)

    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if json_mode:
        print(json.dumps(projects, ensure_ascii=False))
    else:
        if not projects:
            print("No cloud projects found.")
            print("Run 'beacon cloud push' to upload a project.")
            return
        for i, p in enumerate(projects, 1):
            print(f"  {i}. {p['project_id']}: {p['name']}")
            if p.get('objective'):
                print(f"     {p['objective'][:60]}")


def cmd_cloud_push():
    from auth import load_credentials
    creds = load_credentials()
    if creds is None:
        print("Not logged in. Run: beacon auth login")
        sys.exit(1)

    config = _ensure_cloud_config()
    project_id = config["project_id"]
    api_url = config.get("api_url", DEFAULT_API_URL)

    from store_local import LocalStore
    local = LocalStore(get_project_file())
    data = local.load_project()
    core.validate_project(data)

    from api_client import ApiClient
    client = ApiClient(api_url, creds.id_token or creds.token or "")
    try:
        client.put_project(project_id, data)
    except RuntimeError as e:
        print(f"Error: {e}")
        sys.exit(1)
    print(f"Pushed to cloud: projects/{project_id}")

    # Push local documents
    docs_dir = os.path.join(os.path.dirname(get_project_file()) or ".beacon", "documents")
    if os.path.isdir(docs_dir):
        import glob
        md_files = glob.glob(os.path.join(docs_dir, "*.md"))
        for fpath in md_files:
            doc_info = _read_local_doc(fpath)
            try:
                client.put_document(
                    project_id, doc_info["doc_id"],
                    doc_info["title"], doc_info["content"],
                    doc_info.get("scope"),
                )
                print(f"  doc: {doc_info['doc_id']} ({doc_info.get('scope', 'memo')})")
            except RuntimeError as e:
                print(f"  doc error [{doc_info['doc_id']}]: {e}")

    # Push local retros
    retros_dir = os.path.join(os.path.dirname(get_project_file()) or ".beacon", "retro")
    if os.path.isdir(retros_dir):
        import glob
        retro_files = glob.glob(os.path.join(retros_dir, "*.md"))
        for fpath in retro_files:
            week = os.path.basename(fpath)[:-3]  # e.g. "2026-W19"
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
            try:
                client.save_retro(project_id, week, content)
                print(f"  retro: {week}")
            except RuntimeError as e:
                print(f"  retro error [{week}]: {e}")

    # Auto-switch to cloud mode
    beacon_dir = os.path.dirname(get_project_file()) or ".beacon"
    config_path = os.path.join(beacon_dir, "config.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump({"mode": "cloud"}, f, indent=2)
        f.write("\n")
    print("Switched to cloud mode.")


def cmd_cloud_pull():
    client, config = _get_api_client()
    project_id = config["project_id"]

    try:
        data = client.get_project(project_id)
    except RuntimeError as e:
        if "404" in str(e):
            print(f"Project '{project_id}' not found in cloud.")
            print("Run 'beacon cloud push' to upload first.")
        else:
            print(f"Error: {e}")
        sys.exit(1)

    core.validate_project(data)

    from store_local import LocalStore
    local = LocalStore(get_project_file())
    local.save_project(data)
    print(f"Pulled from cloud: projects/{project_id}")


def cmd_cloud_status():
    config_path = _get_cloud_config_path()
    if not os.path.exists(config_path):
        print("Cloud: not configured")
        print("Run 'beacon cloud push' to set up.")
        return

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    from auth import load_credentials
    creds = load_credentials()
    logged_in = creds is not None

    print(f"Cloud: {config['project_id']}")
    print(f"API: {config.get('api_url', DEFAULT_API_URL)}")
    print(f"Auth: {'logged in' if logged_in else 'not logged in'}")


# ---------------------------------------------------------------------------
# PR commands (ms-15)
# ---------------------------------------------------------------------------

def cmd_pr_add():
    # Registers an existing GitHub PR URL into beacon with optional intent annotation.
    import datetime
    url = os.environ.get("BEACON_URL", "")
    ms_id = os.environ.get("BEACON_MS_ID", "")
    intent = os.environ.get("BEACON_INTENT", "")
    author = os.environ.get("BEACON_AUTHOR", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    date = os.environ.get("BEACON_DATE", "") or datetime.date.today().isoformat()

    if not url:
        print("Error: GitHub URL required", file=sys.stderr)
        sys.exit(1)

    if not intent:
        # Interactive prompt if not provided via env
        try:
            intent = input("Intent (why was this PR created?): ").strip()
        except (EOFError, KeyboardInterrupt):
            intent = ""

    data = load_project()
    try:
        eid = core.pr_add(data, ms_id=ms_id, url=url, author=author,
                          intent=intent, date=date)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    save_project(data)

    if json_mode:
        print(json.dumps({"entry_id": eid, "url": url, "intent": intent}, ensure_ascii=False))
    else:
        print(f"Added PR [{eid}]: {url}")
        if intent:
            print(f"  Intent: {intent}")


def cmd_pr_close():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not entry_id:
        print("Error: entry ID required", file=sys.stderr)
        sys.exit(1)

    data = load_project()
    try:
        ms, entry = core.pr_close(data, entry_id)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    save_project(data)

    if json_mode:
        print(json.dumps({"entry_id": entry_id, "pr_status": "closed"}, ensure_ascii=False))
    else:
        print(f"Closed PR [{entry_id}]: {entry.get('description', '')}")


def cmd_pr_approve():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    rationale = os.environ.get("BEACON_RATIONALE", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not entry_id:
        print("Error: entry ID required", file=sys.stderr)
        sys.exit(1)

    if not rationale:
        try:
            rationale = input("Rationale (reason for approval): ").strip()
        except (EOFError, KeyboardInterrupt):
            rationale = ""

    data = load_project()
    try:
        ms, entry = core.pr_approve(data, entry_id, rationale=rationale)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    save_project(data)

    if json_mode:
        print(json.dumps({"entry_id": entry_id, "review_status": "approved",
                          "review_rationale": rationale}, ensure_ascii=False))
    else:
        print(f"Approved PR [{entry_id}]: {entry.get('description', '')}")
        if rationale:
            print(f"  Rationale: {rationale}")


def cmd_pr_reject():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    rationale = os.environ.get("BEACON_RATIONALE", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not entry_id:
        print("Error: entry ID required", file=sys.stderr)
        sys.exit(1)

    if not rationale:
        try:
            rationale = input("Rationale (reason for rejection): ").strip()
        except (EOFError, KeyboardInterrupt):
            rationale = ""

    data = load_project()
    try:
        ms, entry = core.pr_reject(data, entry_id, rationale=rationale)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    save_project(data)

    if json_mode:
        print(json.dumps({"entry_id": entry_id, "review_status": "rejected",
                          "review_rationale": rationale}, ensure_ascii=False))
    else:
        print(f"Rejected PR [{entry_id}]: {entry.get('description', '')}")
        if rationale:
            print(f"  Rationale: {rationale}")


def cmd_pr_request_review():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not entry_id:
        print("Error: entry ID required", file=sys.stderr)
        sys.exit(1)
    data = load_project()
    try:
        ms, entry = core.pr_request_review(data, entry_id)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    save_project(data)
    if json_mode:
        print(json.dumps({"entry_id": entry_id, "pr_status": "in_review"}, ensure_ascii=False))
    else:
        print(f"In review: [{entry_id}]: {entry.get('description', '')}")


def cmd_pr_merge():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not entry_id:
        print("Error: entry ID required", file=sys.stderr)
        sys.exit(1)
    import datetime
    today = datetime.date.today().isoformat()
    data = load_project()
    try:
        ms, entry = core.pr_merge(data, entry_id, date=today)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    save_project(data)
    if json_mode:
        print(json.dumps({"entry_id": entry_id, "pr_status": "merged"}, ensure_ascii=False))
    else:
        print(f"Merged PR [{entry_id}]: {entry.get('description', '')}")


def cmd_pr_create():
    """Wrapper for gh pr create that auto-records the PR in beacon."""
    ms_id = os.environ.get("BEACON_MS_ID", "")
    intent = os.environ.get("BEACON_INTENT", "")
    gh_args = os.environ.get("BEACON_GH_ARGS", "")

    # Run gh pr create and capture the URL from stdout
    cmd = ["gh", "pr", "create"]
    if gh_args:
        import shlex
        cmd += shlex.split(gh_args)

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    if result.returncode != 0:
        sys.exit(result.returncode)

    # Extract PR URL from gh output (last line that looks like a URL)
    pr_url = ""
    for line in reversed(result.stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("https://github.com/") and "/pull/" in line:
            pr_url = line
            break

    if not pr_url:
        print("Warning: could not detect PR URL from gh output", file=sys.stderr)
        return

    if not intent:
        try:
            intent = input(f"Intent for beacon PR record (or Enter to skip): ").strip()
        except (EOFError, KeyboardInterrupt):
            intent = ""

    date = __import__("datetime").date.today().isoformat()
    data = load_project()
    try:
        eid = core.pr_add(data, ms_id=ms_id, url=pr_url, intent=intent, date=date)
    except ValueError as e:
        print(f"Warning: beacon pr record failed: {e}", file=sys.stderr)
        return
    save_project(data)
    print(f"Beacon: PR recorded [{eid}]")


# ---------------------------------------------------------------------------
# Skill install
# ---------------------------------------------------------------------------

def cmd_skill_install():
    """Install beacon Claude Code Skills into ~/.claude/skills/ and update CLAUDE.md."""
    import shutil
    _append_claude_md()

    # Find skills source relative to this file: <beacon_root>/skills/
    beacon_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    skills_src = os.path.join(beacon_root, "skills")

    if not os.path.isdir(skills_src):
        print(f"Error: skills directory not found at {skills_src}")
        sys.exit(1)

    # Destination: ~/.claude/skills/
    home = os.path.expanduser("~")
    claude_skills = os.path.join(home, ".claude", "skills")
    os.makedirs(claude_skills, exist_ok=True)

    installed = []
    for src_file in sorted(os.listdir(skills_src)):
        if not src_file.endswith(".md"):
            continue
        skill_name = src_file[:-3]  # strip .md
        dest_dir = os.path.join(claude_skills, skill_name)
        os.makedirs(dest_dir, exist_ok=True)
        dest_file = os.path.join(dest_dir, "SKILL.md")
        shutil.copy2(os.path.join(skills_src, src_file), dest_file)
        installed.append(skill_name)

    if installed:
        print(f"Installed {len(installed)} Skills to {claude_skills}:")
        for name in installed:
            print(f"  /{name}")
    else:
        print("No skills found to install.")


# ---------------------------------------------------------------------------
# Main dispatch
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    commands = {
        "init": cmd_init,
        "milestone_add": cmd_milestone_add,
        "milestone_list": cmd_milestone_list,
        "milestone_start": cmd_milestone_start,
        "milestone_done": cmd_milestone_done,
        "milestone_show": cmd_milestone_show,
        "milestone_update": cmd_milestone_update,
        "milestone_delete": cmd_milestone_delete,
        "log": cmd_log,
        "log_prepare": cmd_log_prepare,
        "log_finalize": cmd_log_finalize,
        "sync": cmd_sync,
        "task_add": cmd_task_add,
        "task_done": cmd_task_done,
        "task_list": cmd_task_list,
        "task_show": cmd_task_show,
        "task_detail": cmd_task_detail,
        "task_update": cmd_task_update,
        "task_delete": cmd_task_delete,
        "entry_move": cmd_entry_move,
        "summary": cmd_summary,
        "save": cmd_save,
        "milestone_depends": cmd_milestone_depends,
        "milestone_workspace": cmd_milestone_workspace,
        "milestone_graph": cmd_milestone_graph,
        "retro_prepare": cmd_retro_prepare,
        "retro_done": cmd_retro_done,
        "trigger_fire": cmd_trigger_fire,
        "trigger_check": cmd_trigger_check,
        "trigger_clear": cmd_trigger_clear,
        "doc_list": cmd_doc_list,
        "doc_show": cmd_doc_show,
        "doc_add": cmd_doc_add,
        "doc_update": cmd_doc_update,
        "doc_delete": cmd_doc_delete,
        "cloud_list": cmd_cloud_list,
        "cloud_push": cmd_cloud_push,
        "cloud_pull": cmd_cloud_pull,
        "cloud_status": cmd_cloud_status,
        "cloud_join": cmd_cloud_join,
        "common_setup": cmd_common_setup,
        "auth_check": cmd_auth_check,
        "auth_login": lambda: __import__("auth").login(),
        "auth_logout": lambda: __import__("auth").logout(),
        "auth_status": lambda: __import__("auth").status(),
        "skill_install": cmd_skill_install,
        "pr_add": cmd_pr_add,
        "pr_close": cmd_pr_close,
        "pr_approve": cmd_pr_approve,
        "pr_reject": cmd_pr_reject,
        "pr_create": cmd_pr_create,
        "pr_request_review": cmd_pr_request_review,
        "pr_merge": cmd_pr_merge,
        "version": lambda: print(f"beacon {__version__}"),
    }
    fn = commands.get(cmd)
    if fn:
        fn()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
