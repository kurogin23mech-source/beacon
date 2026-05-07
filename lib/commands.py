#!/usr/bin/env python3
"""Beacon CLI commands - called from beacon shell script with env vars."""

import json
import os
import subprocess
import sys


def get_project_file():
    return os.environ.get("BEACON_PROJECT_FILE", ".beacon/project.json")


def load_project():
    with open(get_project_file(), "r", encoding="utf-8") as f:
        return json.load(f)


def save_project(data):
    with open(get_project_file(), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def cmd_init():
    name = os.environ.get("BEACON_NAME", "")
    objective = os.environ.get("BEACON_OBJECTIVE", "")
    pf = get_project_file()
    os.makedirs(os.path.dirname(pf), exist_ok=True)
    data = {"name": name, "objective": objective, "milestones": []}
    with open(pf, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
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
            "status": "planned",
            "target_date": target_date,
            "commits": [],
        }
    )
    save_project(data)
    print(f"Added milestone ms-{ms_id}: {title}")


def cmd_milestone_list():
    data = load_project()
    icons = {"done": "\u25cf", "in_progress": "\u25d0", "planned": "\u25cb", "todo": "\u25cb"}
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
            ms["status"] = "planned"
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
    """Generate next entry id across all milestones."""
    max_id = 0
    for ms in data["milestones"]:
        for entry in ms.get("entries", []):
            eid = entry.get("id", "")
            if eid.startswith("e-"):
                try:
                    max_id = max(max_id, int(eid[2:]))
                except ValueError:
                    pass
    return f"e-{max_id + 1}"


def update_progress(target, progress_str):
    """Update milestone progress if specified."""
    if progress_str:
        try:
            p = int(progress_str)
            target["progress"] = max(0, min(100, p))
            print(f"  Progress: {target['progress']}%")
        except ValueError:
            pass


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
        "status": "done",
        "meta": {"hash": commit_hash, "message": message},
    })
    update_progress(target, progress)
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
            entries.insert(0, {
                "id": next_entry_id(data),
                "type": "commit",
                "description": msg,
                "date": date_str.split(" ")[0],
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

    data = load_project()
    target = find_target_milestone(data, ms_id)

    entries = target.setdefault("entries", [])
    eid = next_entry_id(data)
    entries.append({
        "id": eid,
        "type": entry_type,
        "description": description,
        "date": date,
        "status": "todo",
        "meta": {},
    })
    save_project(data)
    print(f"Added {entry_type} [{eid}] to {target['title']}: {description}")


def cmd_task_done():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    progress = os.environ.get("BEACON_PROGRESS", "")
    data = load_project()

    for ms in data["milestones"]:
        for entry in ms.get("entries", []):
            if entry["id"] == entry_id:
                entry["status"] = "done"
                if not entry.get("date"):
                    import datetime
                    entry["date"] = datetime.date.today().isoformat()
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
        "summary": cmd_summary,
    }
    fn = commands.get(cmd)
    if fn:
        fn()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
