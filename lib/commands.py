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
    icons = {"done": "\u25cf", "in_progress": "\u25d0", "planned": "\u25cb"}
    for ms in data["milestones"]:
        icon = icons.get(ms["status"], "?")
        active = " \u25c0 ACTIVE" if ms["status"] == "in_progress" else ""
        print(f"  {icon} [{ms['id']}] {ms['title']} ({ms.get('target_date', '?')}){active}")


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


def cmd_log():
    summary = os.environ.get("BEACON_SUMMARY", "")
    commit_hash = os.environ.get("BEACON_HASH", "")
    message = os.environ.get("BEACON_MESSAGE", "")
    date = os.environ.get("BEACON_DATE", "")

    data = load_project()
    active = None
    for ms in data["milestones"]:
        if ms["status"] == "in_progress":
            active = ms
            break
    if not active:
        print("No active milestone. Run: beacon milestone start <ms-id>")
        sys.exit(1)

    for c in active["commits"]:
        if c.get("hash", "").startswith(commit_hash):
            print(f"Already logged: {commit_hash}")
            return

    active["commits"].append(
        {"hash": commit_hash, "message": message, "date": date, "summary": summary}
    )
    save_project(data)
    print(f"Logged {commit_hash} to {active['title']}")


def cmd_sync():
    data = load_project()
    active = None
    for ms in data["milestones"]:
        if ms["status"] == "in_progress":
            active = ms
            break
    if not active:
        print("No active milestone.")
        sys.exit(1)

    existing_hashes = {c["hash"][:7] for c in active.get("commits", [])}

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
            active["commits"].insert(
                0,
                {
                    "hash": h,
                    "message": msg,
                    "date": date_str.split(" ")[0],
                    "summary": "",
                },
            )
            added += 1

    if added:
        save_project(data)
        print(f"Synced {added} new commits to: {active['title']}")
    else:
        print("No new commits to sync.")


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
    }
    fn = commands.get(cmd)
    if fn:
        fn()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
