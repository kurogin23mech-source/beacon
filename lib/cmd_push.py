#!/usr/bin/env python3
"""cmd_push.py — the `beacon push *` command family (ms-127 e-4321).

Extracted verbatim from commands.py (god-module split). Depends only on
commands_shared (upward) + leaf domain modules, never on commands.py — acyclic
(SPEC 方針4). commands.py re-imports the PUBLIC handlers for dispatch + `commands.X`;
family-private helpers are NOT re-exported (patch them at cmd_push.<name>).
"""

import json
import os
import sys

import core
import git_read_port
from commands_shared import load_project, save_project

# Read-only git introspection (branch / HEAD / commit range / git user) lives
# behind git_read_port (ms-142 e-5527, spine §5): the outward-effect-free `git`
# calls are the adapter, the handler keeps the record(L2) + business(L3) halves
# incl. the fallback defaults (main / HEAD) which are a business decision.


def _next_push_id(data: dict, date_str: str) -> str:
    """Generate next push ID like push-20260519-1."""
    prefix = f"push-{date_str.replace('-', '')[:8]}"
    nums = []
    for p in data.get("pushes", []):
        if p["id"].startswith(prefix + "-"):
            try:
                nums.append(int(p["id"][len(prefix) + 1:]))
            except ValueError:
                pass
    n = (max(nums) + 1) if nums else 1
    return f"{prefix}-{n}"


def cmd_push_record():
    """Record a push (code release) entry based on commits since last push."""
    mode = os.environ.get("BEACON_MODE", "")          # "prepare" or "finalize" or ""
    from_hash = os.environ.get("BEACON_FROM", "")
    to_hash = os.environ.get("BEACON_TO", "")
    branch = os.environ.get("BEACON_BRANCH", "")
    description = os.environ.get("BEACON_DESCRIPTION", "")
    ms_id = os.environ.get("BEACON_MS", "")
    # version (e-1274): git tag attached to this push (e.g. v0.22.0). Stored
    # alongside branch / commits so later reviews can cross-reference releases
    # without timestamp guessing.
    version = os.environ.get("BEACON_VERSION", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    data = load_project()
    now = core._now_iso()
    today = now[:10]

    # Resolve branch
    if not branch:
        try:
            branch = git_read_port.current_branch()
        except Exception:
            branch = "main"

    # Resolve to_hash
    if not to_hash:
        try:
            to_hash = git_read_port.rev_parse_short("HEAD")
        except Exception:
            to_hash = "HEAD"

    # Resolve from_hash: last recorded push's to_hash, or first commit
    pushes = data.get("pushes", [])
    if not from_hash:
        from_hash = pushes[-1]["to_hash"] if pushes else ""

    # Collect commits in range
    try:
        commits = git_read_port.log_commits(from_hash, to_hash, 50)
    except Exception:
        commits = []

    # Resolve active ms_id if not given
    if not ms_id:
        for ms in data.get("milestones", []):
            if ms.get("status") == "in_progress":
                ms_id = ms["id"]
                break

    # Pushed by: git user
    try:
        pushed_by = git_read_port.config_user_name()
    except Exception:
        pushed_by = ""

    # --- Prepare mode: return context JSON for AI summary generation ---
    if mode == "prepare":
        last_push = pushes[-1] if pushes else None
        payload = {
            "branch": branch,
            "from_hash": from_hash,
            "to_hash": to_hash,
            "commits": commits[:30],
            "ms_id": ms_id,
            "last_push": {"id": last_push["id"], "date": last_push.get("pushed_at", "")} if last_push else None,
        }
        print(json.dumps(payload, ensure_ascii=False))
        return

    # Auto-generate description if not AI-provided
    if not description and commits:
        description = commits[0]["message"] if len(commits) == 1 else f"{len(commits)} commits pushed"

    push_id = _next_push_id(data, today)
    push_entry = {
        "id": push_id,
        "branch": branch,
        "from_hash": from_hash,
        "to_hash": to_hash,
        "commit_count": len(commits),
        "commits": commits,
        "summary": description,
        "pushed_by": pushed_by,
        "pushed_at": now,
        "ms_id": ms_id or None,
    }
    if version:
        # e-1274: top-level version tag for cross-referencing with releases.
        push_entry["version"] = version

    data.setdefault("pushes", []).append(push_entry)
    save_project(data)

    if json_mode:
        print(json.dumps({"push": push_entry}, ensure_ascii=False))
    else:
        ver_str = f"  {version}" if version else ""
        print(f"↑ {push_id}{ver_str}  {branch}  {from_hash or '(initial)'}..{to_hash}  ({len(commits)} commits)")
        if description:
            print(f"  {description}")


def cmd_push_list():
    """List push records."""
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()
    pushes = data.get("pushes", [])

    if json_mode:
        print(json.dumps({"pushes": pushes}, ensure_ascii=False))
        return

    if not pushes:
        print("No push records.")
        return
    for p in reversed(pushes):
        date = p.get("pushed_at", "")[:16].replace("T", " ")
        print(f"↑ {p['id']}  {p['branch']}  {p.get('pushed_by', '')}  {date}  ({p.get('commit_count', 0)} commits)")
        if p.get("summary"):
            print(f"  {p['summary']}")
