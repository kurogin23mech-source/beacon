#!/usr/bin/env python3
"""Beacon CLI commands - thin adapter over core.py logic."""

__version__ = "0.1.0"

import json
import os
import re
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
    content = ""
    if os.path.exists(claude_md):
        with open(claude_md, "r", encoding="utf-8") as f:
            content = f.read()
        if marker in content:
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
    for entry in post_tool_use:
        if entry.get("matcher") == "Bash":
            for h in entry.get("hooks", []):
                cmd = h.get("command", "")
                if "beacon-post-commit-hook" in cmd or "beacon log --prepare" in cmd:
                    return
    post_tool_use.append({
        "matcher": "Bash",
        "hooks": [{
            "type": "command",
            "command": CLAUDE_HOOK_SCRIPT,
            "timeout": 10,
            "statusMessage": "Beacon: checking commit...",
        }],
    })
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print("Installed Claude Code PostToolUse hook")


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
    _append_claude_md()
    _install_git_hook()
    _install_claude_hook()
    print(f"Created {pf}")
    print("Next: beacon milestone add")


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
    data = load_project()

    milestones = data["milestones"]
    if not show_all:
        milestones = [ms for ms in milestones if ms.get("status") != "cancelled"]

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
        targets = [ms for ms in data["milestones"] if ms["status"] in ("in_progress", "observing")]
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

    data = load_project()
    target = core.find_target_milestone(data, ms_id)
    eid = core.task_add(data, ms_id, description, entry_type=entry_type, date=date, detail=detail)
    save_project(data)
    print(f"Added {entry_type} [{eid}] to {target['title']}: {description}")


def cmd_task_done():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    progress = os.environ.get("BEACON_PROGRESS", "")
    import datetime
    today = datetime.date.today().isoformat()

    data = load_project()
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
    data = load_project()
    target = core.find_target_milestone(data, ms_id)

    entries = core.filter_cancelled(target.get("entries", []), show_all)

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
    detail = entry.get("detail", "")
    if detail:
        print(f"\n{detail}")
    else:
        print("\n(no detail)")


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
# Cloud commands
# ---------------------------------------------------------------------------

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

    config = {"project_id": project_id}
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"Created {config_path} (project_id: {project_id})")
    return config


def cmd_cloud_list():
    """List cloud projects for selection."""
    from store_firestore import FirestoreStore
    from auth import load_credentials
    creds = load_credentials()
    if creds is None:
        print("Not logged in. Run: beacon auth login")
        sys.exit(1)

    from google.cloud import firestore
    db = firestore.Client(project="beacon-cloud-96f5f", credentials=creds)
    docs = db.collection("projects").stream()
    projects = []
    for doc in docs:
        data = doc.to_dict()
        projects.append({
            "project_id": doc.id,
            "name": data.get("name", ""),
            "objective": data.get("objective", ""),
        })

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
            if p['objective']:
                print(f"     {p['objective'][:60]}")


def cmd_cloud_push():
    from auth import load_credentials
    creds = load_credentials()
    if creds is None:
        print("Not logged in. Run: beacon auth login")
        sys.exit(1)

    config = _ensure_cloud_config()
    project_id = config["project_id"]

    from store_local import LocalStore
    local = LocalStore(get_project_file())
    data = local.load_project()
    core.validate_project(data)

    from store_firestore import FirestoreStore
    store = FirestoreStore(project_id)
    store.save_project(data)
    print(f"Pushed to Firestore: projects/{project_id}")

    # Auto-switch to cloud mode
    beacon_dir = os.path.dirname(get_project_file()) or ".beacon"
    config_path = os.path.join(beacon_dir, "config.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump({"mode": "cloud"}, f, indent=2)
        f.write("\n")
    print("Switched to cloud mode.")


def cmd_cloud_pull():
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
    project_id = config["project_id"]

    from store_firestore import FirestoreStore
    store = FirestoreStore(project_id)
    try:
        data = store.load_project()
    except FileNotFoundError:
        print(f"Project '{project_id}' not found in Firestore.")
        print("Run 'beacon cloud push' to upload first.")
        sys.exit(1)

    core.validate_project(data)

    from store_local import LocalStore
    local = LocalStore(get_project_file())
    local.save_project(data)
    print(f"Pulled from Firestore: projects/{project_id}")


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
    print(f"Auth: {'logged in' if logged_in else 'not logged in'}")


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
        "retro_prepare": cmd_retro_prepare,
        "retro_done": cmd_retro_done,
        "trigger_fire": cmd_trigger_fire,
        "trigger_check": cmd_trigger_check,
        "trigger_clear": cmd_trigger_clear,
        "cloud_list": cmd_cloud_list,
        "cloud_push": cmd_cloud_push,
        "cloud_pull": cmd_cloud_pull,
        "cloud_status": cmd_cloud_status,
        "auth_login": lambda: __import__("auth").login(),
        "auth_logout": lambda: __import__("auth").logout(),
        "auth_status": lambda: __import__("auth").status(),
        "version": lambda: print(f"beacon {__version__}"),
    }
    fn = commands.get(cmd)
    if fn:
        fn()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
