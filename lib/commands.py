#!/usr/bin/env python3
"""Beacon CLI commands - called from beacon shell script with env vars."""

import json
import os
import subprocess
import sys


VALID_STATUSES = {"todo", "in_progress", "in_review", "waiting", "done"}
VALID_ENTRY_TYPES = {"commit", "task", "note"}


def get_project_file():
    return os.environ.get("BEACON_PROJECT_FILE", ".beacon/project.json")


def validate_project(data):
    """Validate project.json schema. Raises ValueError on invalid data."""
    if not isinstance(data, dict):
        raise ValueError("project.json must be a JSON object")
    for key in ("name", "milestones"):
        if key not in data:
            raise ValueError(f"Missing required field: {key}")
    if not isinstance(data["milestones"], list):
        raise ValueError("milestones must be an array")

    for ms in data["milestones"]:
        if "tasks" in ms:
            raise ValueError(
                f"Milestone '{ms.get('id', '?')}' uses 'tasks' field. "
                "Use 'entries' instead. Do NOT edit project.json directly — use beacon CLI."
            )
        if ms.get("status") and ms["status"] not in VALID_STATUSES:
            raise ValueError(
                f"Milestone '{ms.get('id', '?')}' has invalid status '{ms['status']}'. "
                f"Valid: {', '.join(sorted(VALID_STATUSES))}"
            )
        for entry in ms.get("entries", []):
            _validate_entry(entry, ms.get("id", "?"))


def _validate_entry(entry, ms_id):
    """Recursively validate an entry and its children."""
    if entry.get("type") and entry["type"] not in VALID_ENTRY_TYPES:
        raise ValueError(
            f"Entry '{entry.get('id', '?')}' in {ms_id} has invalid type '{entry['type']}'. "
            f"Valid: {', '.join(sorted(VALID_ENTRY_TYPES))}"
        )
    if entry.get("status") and entry["status"] not in VALID_STATUSES:
        raise ValueError(
            f"Entry '{entry.get('id', '?')}' in {ms_id} has invalid status '{entry['status']}'. "
            f"Valid: {', '.join(sorted(VALID_STATUSES))}"
        )
    for child in entry.get("entries", []):
        _validate_entry(child, ms_id)


def load_project():
    with open(get_project_file(), "r", encoding="utf-8") as f:
        data = json.load(f)
    validate_project(data)
    return data


def save_project(data):
    validate_project(data)
    with open(get_project_file(), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


CLAUDE_MD_BEACON_SECTION = """\

## Beacon プロジェクト管理

プロジェクト進捗は `.beacon/project.json` を参照。セッション開始時やコミット時に確認すること。

- **`.beacon/project.json` を直接編集してはいけない。必ず beacon CLI コマンドを使うこと**
  - マイルストーン追加: `beacon milestone add`
  - タスク追加: `beacon task add "説明" -m <ms-id>`
  - タスク完了: `beacon task done <entry-id>`
  - 進捗記録: `beacon log "概要"`
  - 同期: `beacon sync`
- 実装を開始する前に、必ず `.beacon/project.json` のマイルストーン一覧を確認し、「この作業はどのマイルストーンに向かうものか」をユーザーに確認すること
- コミット後は `beacon log "概要"` でアクティブマイルストーンに記録する
- 同じ課題に2回以上コミットが発生したら、タスクにまとめることを提案する
- マイルストーンの追加・完了は `beacon milestone` コマンドで管理する
"""


def _append_claude_md():
    """Append beacon section to CLAUDE.md if not already present."""
    claude_md = "CLAUDE.md"
    marker = "## Beacon プロジェクト管理"

    content = ""
    if os.path.exists(claude_md):
        with open(claude_md, "r", encoding="utf-8") as f:
            content = f.read()
        if marker in content:
            return  # Already present

    with open(claude_md, "a", encoding="utf-8") as f:
        f.write(CLAUDE_MD_BEACON_SECTION)
    print(f"Updated {claude_md} with beacon rules")


def cmd_init():
    name = os.environ.get("BEACON_NAME", "")
    objective = os.environ.get("BEACON_OBJECTIVE", "")
    pf = get_project_file()
    os.makedirs(os.path.dirname(pf), exist_ok=True)
    data = {"name": name, "objective": objective, "milestones": []}
    with open(pf, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    _append_claude_md()
    print(f"Created {pf}")
    print("Next: beacon milestone add")


def cmd_milestone_add():
    title = os.environ.get("BEACON_TITLE", "")
    target_date = os.environ.get("BEACON_TARGET_DATE", "")
    data = load_project()
    ms_id = len(data["milestones"]) + 1
    data["milestones"].append(
        {
            "id": f"ms-{ms_id}",
            "title": title,
            "status": "todo",
            "target_date": target_date,
            "commits": [],
        }
    )
    save_project(data)
    print(f"Added milestone ms-{ms_id}: {title}")


def cmd_milestone_list():
    data = load_project()
    icons = {"done": "\u25cf", "in_progress": "\u25d0", "todo": "\u25cb", "waiting": "\u25cc", "in_review": "\u25d1"}
    for ms in data["milestones"]:
        icon = icons.get(ms["status"], "?")
        active = " \u25c0 ACTIVE" if ms["status"] == "in_progress" else ""
        progress = ms.get("progress", 0)
        print(f"  {icon} [{ms['id']}] {ms['title']} ({progress}%){active}")


def cmd_milestone_start():
    ms_id = os.environ.get("BEACON_MS_ID", "")
    data = load_project()
    found = False
    for ms in data["milestones"]:
        if ms["status"] == "in_progress":
            ms["status"] = "todo"
        if ms["id"] == ms_id:
            ms["status"] = "in_progress"
            found = True
            print(f"Activated: {ms['title']}")
    if not found:
        print(f"Milestone not found: {ms_id}")
        sys.exit(1)
    save_project(data)


def cmd_milestone_done():
    ms_id = os.environ.get("BEACON_MS_ID", "")
    data = load_project()
    for ms in data["milestones"]:
        if ms["id"] == ms_id:
            ms["status"] = "done"
            print(f"Completed: {ms['title']}")
            save_project(data)
            return
    print(f"Milestone not found: {ms_id}")
    sys.exit(1)


def find_target_milestone(data, ms_id):
    """Find target milestone by id or auto-select if only one is active."""
    if ms_id:
        for ms in data["milestones"]:
            if ms["id"] == ms_id:
                return ms
        print(f"Milestone not found: {ms_id}")
        sys.exit(1)
    else:
        active_list = [ms for ms in data["milestones"] if ms["status"] == "in_progress"]
        if len(active_list) == 0:
            print("No active milestone. Run: beacon milestone start <ms-id>")
            sys.exit(1)
        elif len(active_list) > 1:
            print("Multiple active milestones. Specify with -m <ms-id>:")
            for ms in active_list:
                print(f"  {ms['id']}: {ms['title']}")
            sys.exit(1)
        return active_list[0]


def next_entry_id(data):
    """Generate next entry id across all milestones (including nested)."""
    max_id = 0
    for ms in data["milestones"]:
        for entry in ms.get("entries", []):
            max_id = _max_entry_id(entry, max_id)
    return f"e-{max_id + 1}"


def _max_entry_id(entry, current_max):
    """Recursively find max entry id."""
    eid = entry.get("id", "")
    if eid.startswith("e-"):
        try:
            current_max = max(current_max, int(eid[2:]))
        except ValueError:
            pass
    for child in entry.get("entries", []):
        current_max = _max_entry_id(child, current_max)
    return current_max


def find_entry(data, entry_id):
    """Find an entry by ID across all milestones (including nested).
    Returns (milestone, parent_entries_list, entry, index) or None."""
    for ms in data["milestones"]:
        result = _find_entry_in(ms.get("entries", []), entry_id, ms)
        if result:
            return result
    return None


def _find_entry_in(entries, entry_id, ms):
    """Recursively find entry. Returns (ms, parent_list, entry, index)."""
    for i, entry in enumerate(entries):
        if entry.get("id") == entry_id:
            return (ms, entries, entry, i)
        # Search in nested entries
        children = entry.get("entries", [])
        result = _find_entry_in(children, entry_id, ms)
        if result:
            return result
    return None


def update_progress(target, progress_str):
    """Update milestone progress if specified. Auto-transitions status."""
    if progress_str:
        try:
            p = int(progress_str)
            target["progress"] = max(0, min(100, p))
            print(f"  Progress: {target['progress']}%")
        except ValueError:
            return

    # Auto-transition: todo → in_progress when progress > 0
    p = target.get("progress", 0)
    if p > 0 and target.get("status") == "todo":
        target["status"] = "in_progress"
    # waiting → in_progress requires user confirmation
    elif p > 0 and target.get("status") == "waiting":
        try:
            answer = input(f"  '{target['title']}' is waiting. Move to in_progress? [y/N]: ").strip().lower()
            if answer in ("y", "yes"):
                target["status"] = "in_progress"
        except (EOFError, KeyboardInterrupt):
            pass


def update_summary(data, target, last_action):
    """Auto-update project summary based on current state."""
    active = [ms for ms in data["milestones"] if ms["status"] == "in_progress"]
    parts = []
    for ms in active:
        parts.append(f"{ms['id']}({ms['title']}, {ms.get('progress', 0)}%)")
    status_part = "実行中: " + ", ".join(parts) if parts else "アクティブMSなし"
    data["summary"] = f"{status_part}。直近: {last_action}"


def cmd_log():
    summary = os.environ.get("BEACON_SUMMARY", "")
    commit_hash = os.environ.get("BEACON_HASH", "")
    message = os.environ.get("BEACON_MESSAGE", "")
    date = os.environ.get("BEACON_DATE", "")
    ms_id = os.environ.get("BEACON_MS_ID", "")
    progress = os.environ.get("BEACON_PROGRESS", "")

    data = load_project()
    target = find_target_milestone(data, ms_id)

    entries = target.setdefault("entries", [])
    for entry in entries:
        if entry.get("type") == "commit" and entry.get("meta", {}).get("hash", "").startswith(commit_hash):
            print(f"Already logged: {commit_hash}")
            # Still allow progress update even if already logged
            if progress:
                update_progress(target, progress)
                save_project(data)
            return

    entries.append({
        "id": next_entry_id(data),
        "type": "commit",
        "description": summary or message,
        "date": date,
        "created_at": date,
        "done_at": date,
        "status": "done",
        "meta": {"hash": commit_hash, "message": message},
    })
    update_progress(target, progress)
    update_summary(data, target, summary or message)
    save_project(data)
    print(f"Logged {commit_hash} to {target['title']}")


def cmd_sync():
    ms_id = os.environ.get("BEACON_MS_ID", "")
    data = load_project()
    target = find_target_milestone(data, ms_id)

    entries = target.setdefault("entries", [])
    existing_hashes = set()
    for entry in entries:
        if entry.get("type") == "commit":
            h = entry.get("meta", {}).get("hash", "")
            if h:
                existing_hashes.add(h[:7])

    result = subprocess.run(
        ["git", "log", "--oneline", "-20", "--pretty=format:%h|%s|%ci"],
        capture_output=True,
        text=True,
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
                "id": next_entry_id(data),
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


def cmd_task_add():
    description = os.environ.get("BEACON_DESCRIPTION", "")
    ms_id = os.environ.get("BEACON_MS_ID", "")
    entry_type = os.environ.get("BEACON_TYPE", "task")
    date = os.environ.get("BEACON_DATE", "")
    detail = os.environ.get("BEACON_DETAIL", "")

    data = load_project()
    target = find_target_milestone(data, ms_id)

    entries = target.setdefault("entries", [])
    eid = next_entry_id(data)
    entry = {
        "id": eid,
        "type": entry_type,
        "description": description,
        "date": date,
        "created_at": date,
        "done_at": None,
        "status": "todo",
        "meta": {},
    }
    if detail:
        entry["detail"] = detail
    entries.append(entry)
    save_project(data)
    print(f"Added {entry_type} [{eid}] to {target['title']}: {description}")


def cmd_task_done():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    progress = os.environ.get("BEACON_PROGRESS", "")
    data = load_project()

    for ms in data["milestones"]:
        for entry in ms.get("entries", []):
            if entry["id"] == entry_id:
                import datetime
                today = datetime.date.today().isoformat()
                entry["status"] = "done"
                entry["done_at"] = today
                if not entry.get("date"):
                    entry["date"] = today
                print(f"Done: [{entry_id}] {entry['description']}")
                update_progress(ms, progress)
                save_project(data)
                return

    print(f"Entry not found: {entry_id}")
    sys.exit(1)


def cmd_task_list():
    ms_id = os.environ.get("BEACON_MS_ID", "")
    data = load_project()
    target = find_target_milestone(data, ms_id)

    entries = target.get("entries", [])
    if not entries:
        print(f"No entries in {target['title']}")
        return

    icons = {"done": "\u25cf", "todo": "\u25cb"}
    for entry in entries:
        icon = icons.get(entry.get("status", "todo"), "?")
        etype = entry.get("type", "?")
        print(f"  {icon} [{entry['id']}] ({etype}) {entry['description']}")


def cmd_task_show():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    data = load_project()
    result = find_entry(data, entry_id)
    if not result:
        print(f"Entry not found: {entry_id}")
        sys.exit(1)
    ms, _, entry, _ = result
    icons = {"done": "\u25cf", "todo": "\u25cb", "in_progress": "\u25d0",
             "waiting": "\u25cc", "in_review": "\u25d1"}
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
    result = find_entry(data, entry_id)
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


def cmd_entry_move():
    """Move an entry under a task entry (grouping)."""
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    task_id = os.environ.get("BEACON_TASK_ID", "")

    if not entry_id or not task_id:
        print("Usage: beacon entry move <entry-id> -t <task-id>")
        sys.exit(1)

    data = load_project()

    # Find the entry to move
    src = find_entry(data, entry_id)
    if not src:
        print(f"Entry not found: {entry_id}")
        sys.exit(1)
    _, src_list, entry, src_idx = src

    # Find the target task
    dst = find_entry(data, task_id)
    if not dst:
        print(f"Task not found: {task_id}")
        sys.exit(1)
    _, _, task_entry, _ = dst

    if task_entry.get("id") == entry_id:
        print("Cannot move entry under itself")
        sys.exit(1)

    # Remove from source
    src_list.pop(src_idx)

    # Add to target task's entries
    task_entry.setdefault("entries", []).append(entry)

    save_project(data)
    print(f"Moved [{entry_id}] under [{task_id}] {task_entry.get('description', '')}")


def cmd_summary():
    text = os.environ.get("BEACON_SUMMARY_TEXT", "")
    data = load_project()
    if text:
        data["summary"] = text
        save_project(data)
        print(f"Summary updated.")
    else:
        print(data.get("summary", "(未設定)"))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    commands = {
        "init": cmd_init,
        "milestone_add": cmd_milestone_add,
        "milestone_list": cmd_milestone_list,
        "milestone_start": cmd_milestone_start,
        "milestone_done": cmd_milestone_done,
        "log": cmd_log,
        "sync": cmd_sync,
        "task_add": cmd_task_add,
        "task_done": cmd_task_done,
        "task_list": cmd_task_list,
        "task_show": cmd_task_show,
        "task_detail": cmd_task_detail,
        "entry_move": cmd_entry_move,
        "summary": cmd_summary,
    }
    fn = commands.get(cmd)
    if fn:
        fn()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
