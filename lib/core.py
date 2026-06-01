"""Beacon Core - Pure business logic for project data manipulation.

All functions take a project data dict and explicit parameters,
returning results without performing I/O. This module is shared
between the CLI (commands.py) and the API (server/app.py).
"""

from __future__ import annotations

import datetime as _dt
import re


def _now_iso() -> str:
    """Return current UTC time as ISO8601 string with seconds precision."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_actor() -> str:
    """Return the current operator: 'claude' if running inside Claude Code, else git user email."""
    import os as _os
    if _os.environ.get("BEACON_CLAUDE_CODE") == "1":
        return "claude"
    try:
        import subprocess
        r = subprocess.run(["git", "config", "user.email"],
                           capture_output=True, text=True, timeout=2)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return _os.environ.get("USER", _os.environ.get("USERNAME", "unknown"))


VALID_STATUSES = {"todo", "in_progress", "in_review", "approved", "waiting", "done", "observing", "cancelled"}
VALID_ENTRY_TYPES = {"commit", "task", "note", "save", "pr", "run_record", "incident", "operation_task"}

# PR lifecycle: in_review → approved → merged (or closed/rejected)
# "open" is reserved for Phase 2 auto-detection via GitHub API (external PRs not yet picked up by beacon)
VALID_PR_STATUSES = {"open", "in_review", "approved", "merged", "closed"}
VALID_REVIEW_STATUSES = {"pending", "approved", "changes_requested", "rejected"}
VALID_RUN_STATUSES = {"ok", "warning", "error"}
VALID_INCIDENT_STATUSES = {"open", "resolved"}
VALID_PRIORITIES = {"highest", "high", "middle", "low", "lowest"}
# Member roles (e-624): defines permissions in 2-5 person team context.
# - owner: project owner, all permissions including delete project
# - maintainer: can manage milestones / merge PRs / approve operations
# - contributor: can create tasks / PRs but not delete or merge
# - viewer: read-only (Web UI dashboard observer)
VALID_MEMBER_ROLES = {"owner", "maintainer", "contributor", "viewer"}
# Operation lifecycle states (mirrors Milestone for symmetry):
#   todo         — defined but not active (outline only, OperationTasks not finished)
#   in_progress  — actively preparing (filling OperationTasks)
#   open         — fully activated, run_records flow in
#   closed       — role finished
VALID_OPERATION_STATUSES = {"todo", "in_progress", "open", "closed"}
VALID_OP_STATUSES = VALID_OPERATION_STATUSES  # alias
MS_ID_RE = re.compile(r"^ms-\d+$")
ENTRY_ID_RE = re.compile(r"^e-\d+$")
OP_ID_RE = re.compile(r"^op-\d+$")

SCHEDULE_DAYS = {
    "daily":    ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
    "weekdays": ["mon", "tue", "wed", "thu", "fri"],
    "weekly":   ["fri"],
}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_project(data: dict) -> None:
    """Validate project.json schema. Raises ValueError on invalid data."""
    if not isinstance(data, dict):
        raise ValueError("project.json must be a JSON object")
    for key in ("name", "milestones"):
        if key not in data:
            raise ValueError(f"Missing required field: {key}")
    if not isinstance(data["milestones"], list):
        raise ValueError("milestones must be an array")

    for ms in data["milestones"]:
        ms_id = ms.get("id", "")
        if ms_id and not MS_ID_RE.match(ms_id):
            raise ValueError(
                f"Milestone ID '{ms_id}' does not match required format 'ms-{{N}}'. "
                "IDs must be ms-1, ms-2, etc."
            )
        if "tasks" in ms:
            raise ValueError(
                f"Milestone '{ms_id or '?'}' uses 'tasks' field. "
                "Use 'entries' instead. Do NOT edit project.json directly — use beacon CLI."
            )
        if ms.get("status") and ms["status"] not in VALID_STATUSES:
            raise ValueError(
                f"Milestone '{ms.get('id', '?')}' has invalid status '{ms['status']}'. "
                f"Valid: {', '.join(sorted(VALID_STATUSES))}"
            )
        for entry in ms.get("entries", []):
            _validate_entry(entry, ms.get("id", "?"))

    for op in data.get("operations", []):
        op_id = op.get("id", "")
        if op_id and not OP_ID_RE.match(op_id):
            raise ValueError(
                f"Operation ID '{op_id}' does not match required format 'op-{{N}}'. "
                "IDs must be op-1, op-2, etc."
            )
        if op.get("status") and op["status"] not in VALID_OPERATION_STATUSES:
            raise ValueError(
                f"Operation '{op_id or '?'}' has invalid status '{op['status']}'. "
                f"Valid: {', '.join(sorted(VALID_OPERATION_STATUSES))}"
            )


def _validate_entry(entry: dict, ms_id: str) -> None:
    """Recursively validate an entry and its children."""
    eid = entry.get("id", "")
    if eid and not ENTRY_ID_RE.match(eid):
        raise ValueError(
            f"Entry ID '{eid}' in {ms_id} does not match required format 'e-{{N}}'. "
            "IDs must be e-1, e-2, etc."
        )
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


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

def find_target_milestone(data: dict, ms_id: str = "") -> dict:
    """Find target milestone by id or auto-select if only one is active.

    Raises ValueError if not found or ambiguous.
    """
    if ms_id:
        for ms in data["milestones"]:
            if ms["id"] == ms_id:
                return ms
        raise ValueError(f"Milestone not found: {ms_id}")

    active_list = [ms for ms in data["milestones"] if ms["status"] == "in_progress"]
    if len(active_list) == 0:
        raise ValueError("No active milestone. Run: beacon milestone start <ms-id>")
    if len(active_list) > 1:
        ids = ", ".join(ms["id"] for ms in active_list)
        raise ValueError(f"Multiple active milestones. Specify with -m <ms-id>: {ids}")
    return active_list[0]


def next_entry_id(data: dict) -> str:
    """Generate next entry id across all milestones and operations (including nested)."""
    max_id = 0
    for ms in data["milestones"]:
        for entry in ms.get("entries", []):
            max_id = _max_entry_id(entry, max_id)
    for op in data.get("operations", []):
        for entry in op.get("entries", []):
            max_id = _max_entry_id(entry, max_id)
    return f"e-{max_id + 1}"


def next_op_id(data: dict) -> str:
    """Generate next operation id."""
    max_id = 0
    for op in data.get("operations", []):
        oid = op.get("id", "")
        if oid.startswith("op-"):
            try:
                max_id = max(max_id, int(oid[3:]))
            except ValueError:
                pass
    return f"op-{max_id + 1}"


def _max_entry_id(entry: dict, current_max: int) -> int:
    eid = entry.get("id", "")
    if eid.startswith("e-"):
        try:
            current_max = max(current_max, int(eid[2:]))
        except ValueError:
            pass
    for child in entry.get("entries", []):
        current_max = _max_entry_id(child, current_max)
    return current_max


def find_entry(data: dict, entry_id: str):
    """Find an entry by ID across all milestones and operations (including nested).

    Returns (container, parent_entries_list, entry, index) or None.
    Container is either a milestone dict or an operation dict.
    """
    for ms in data["milestones"]:
        result = _find_entry_in(ms.get("entries", []), entry_id, ms)
        if result:
            return result
    for op in data.get("operations", []):
        result = _find_entry_in(op.get("entries", []), entry_id, op)
        if result:
            return result
    return None


def _find_entry_in(entries: list, entry_id: str, ms: dict):
    for i, entry in enumerate(entries):
        if entry.get("id") == entry_id:
            return (ms, entries, entry, i)
        children = entry.get("entries", [])
        result = _find_entry_in(children, entry_id, ms)
        if result:
            return result
    return None


# ---------------------------------------------------------------------------
# Milestone operations
# ---------------------------------------------------------------------------

def milestone_add(data: dict, title: str, target_date: str = "",
                   description: str = "", priority: str = "",
                   objective: str = "", acceptance_criteria: str = "",
                   owner: str = "", assignee: str = "") -> str:
    """Add a milestone. Returns the new ms_id."""
    ms_id_num = len(data["milestones"]) + 1
    ms_id = f"ms-{ms_id_num}"
    ms = {
        "id": ms_id,
        "title": title,
        "status": "todo",
        "target_date": target_date,
        "commits": [],
        "created_by": _get_actor(),
        "created_at": _now_iso(),
    }
    if description:
        ms["description"] = description
    if priority:
        if priority not in VALID_PRIORITIES:
            raise ValueError(f"Invalid priority: {priority}. Valid: {', '.join(sorted(VALID_PRIORITIES))}")
        ms["priority"] = priority
    if objective:
        ms["objective"] = objective
    if acceptance_criteria:
        ms["acceptance_criteria"] = acceptance_criteria
    # e-625: owner/assignee are recorded but NOT validated against members[]
    # at this layer — that check belongs in the CLI/API surface, where we
    # can produce a friendly "did you mean to `beacon member add` first?"
    # message. Here we just store whatever string the caller provided.
    if owner:
        ms["owner"] = owner
    if assignee:
        ms["assignee"] = assignee
    data["milestones"].append(ms)
    return ms_id


def milestone_start(data: dict, ms_id: str) -> dict:
    """Activate a milestone (multiple can be active simultaneously). Returns the activated ms."""
    found = None
    for ms in data["milestones"]:
        if ms["id"] == ms_id:
            ms["status"] = "in_progress"
            found = ms
    if not found:
        raise ValueError(f"Milestone not found: {ms_id}")
    return found


def milestone_done(data: dict, ms_id: str, *, reason: str = "") -> dict:
    """Mark a milestone as done. Returns the milestone."""
    for ms in data["milestones"]:
        if ms["id"] == ms_id:
            ms["status"] = "done"
            meta = ms.setdefault("meta", {})
            meta["done_at"] = _now_iso()
            meta["done_by"] = _get_actor()
            if reason:
                meta["done_reason"] = reason
            return ms
    raise ValueError(f"Milestone not found: {ms_id}")


def milestone_update(data: dict, ms_id: str, *,
                     title: str = "", progress: str = "",
                     target_date: str = "", status: str = "",
                     description: str = "", reason: str = "",
                     priority: str = "", objective: str = "",
                     acceptance_criteria: str = "",
                     owner: str = "", assignee: str = "") -> dict:
    """Update milestone fields. Returns the milestone.

    e-625: owner / assignee can be cleared by passing the literal string
    "-" (matches the convention used by other CLI clear-this-field tools);
    any other non-empty value sets the field.
    """
    for ms in data["milestones"]:
        if ms["id"] == ms_id:
            if title:
                ms["title"] = title
            if description is not None and description != "":
                ms["description"] = description
            if progress:
                try:
                    ms["progress"] = max(0, min(100, int(progress)))
                except ValueError:
                    pass
            if target_date:
                ms["target_date"] = target_date
            if priority:
                if priority not in VALID_PRIORITIES:
                    raise ValueError(f"Invalid priority: {priority}. Valid: {', '.join(sorted(VALID_PRIORITIES))}")
                ms["priority"] = priority
            if objective:
                ms["objective"] = objective
            if acceptance_criteria:
                ms["acceptance_criteria"] = acceptance_criteria
            if owner:
                ms["owner"] = "" if owner == "-" else owner
            if assignee:
                ms["assignee"] = "" if assignee == "-" else assignee
            if status:
                if status not in VALID_STATUSES:
                    raise ValueError(
                        f"Invalid status: {status}. Valid: {', '.join(sorted(VALID_STATUSES))}"
                    )
                ms["status"] = status
                meta = ms.setdefault("meta", {})
                meta[f"{status}_at"] = _now_iso()
                meta[f"{status}_by"] = _get_actor()
                if reason:
                    meta[f"{status}_reason"] = reason
            return ms
    raise ValueError(f"Milestone not found: {ms_id}")


def milestone_delete(data: dict, ms_id: str, *, reason: str = "") -> dict:
    """Cancel a milestone (soft delete). Returns the milestone."""
    for ms in data["milestones"]:
        if ms["id"] == ms_id:
            ms["status"] = "cancelled"
            meta = ms.setdefault("meta", {})
            meta["cancelled_at"] = _now_iso()
            meta["cancelled_by"] = _get_actor()
            if reason:
                meta["cancel_reason"] = reason
            return ms
    raise ValueError(f"Milestone not found: {ms_id}")


# ---------------------------------------------------------------------------
# Member operations (e-624)
# ---------------------------------------------------------------------------
#
# `members` is a flat list at project root. Each member is:
#   {
#     "id":       "<handle>",          # required, unique within the project
#     "name":     "<display name>",     # optional, defaults to id
#     "email":    "<email>",            # optional
#     "role":     "owner|maintainer|contributor|viewer",
#     "added_at": "<iso-timestamp>",
#     "added_by": "<actor>",
#   }
#
# These functions are PURE — they mutate the project dict and return a value
# but never touch I/O. They are designed to be called from inside an
# apply_operation closure.

def members_list(data: dict) -> list:
    """Return the list of members (never None — empty list if absent)."""
    members = data.get("members")
    if not isinstance(members, list):
        return []
    return members


def member_add(data: dict, member_id: str, *, name: str = "", email: str = "",
               role: str = "contributor") -> dict:
    """Add a member to the project. Returns the new member dict.

    Raises ValueError if member_id is empty, role is invalid, or a member
    with the same id already exists.
    """
    if not member_id or not member_id.strip():
        raise ValueError("Member id is required")
    member_id = member_id.strip()
    if role not in VALID_MEMBER_ROLES:
        raise ValueError(
            f"Invalid role: {role}. Valid: {', '.join(sorted(VALID_MEMBER_ROLES))}"
        )
    members = data.setdefault("members", [])
    if not isinstance(members, list):
        # Heal a corrupt members field rather than raising — this only happens
        # if someone hand-edited project.json (which is explicitly banned).
        members = []
        data["members"] = members
    for m in members:
        if isinstance(m, dict) and m.get("id") == member_id:
            raise ValueError(f"Member already exists: {member_id}")
    new_member = {
        "id": member_id,
        "name": name or member_id,
        "email": email,
        "role": role,
        "added_at": _now_iso(),
        "added_by": _get_actor(),
    }
    members.append(new_member)
    return new_member


def member_remove(data: dict, member_id: str, *, reason: str = "") -> dict:
    """Remove a member from the project. Returns the removed member dict.

    Raises ValueError if no such member exists. owner role cannot be removed
    (escalate the new owner first with `member role`).
    """
    members = data.get("members", [])
    if not isinstance(members, list):
        raise ValueError(f"Member not found: {member_id}")
    for i, m in enumerate(members):
        if isinstance(m, dict) and m.get("id") == member_id:
            if m.get("role") == "owner":
                raise ValueError(
                    f"Cannot remove owner {member_id}. "
                    "Promote another member with `beacon member role <id> owner` first."
                )
            removed = members.pop(i)
            # Audit trail lives in changelog.jsonl via apply_operation; we
            # don't write that here (this function is pure data).
            return removed
    raise ValueError(f"Member not found: {member_id}")


def member_set_role(data: dict, member_id: str, role: str) -> dict:
    """Change a member's role. Returns the updated member dict.

    Raises ValueError on invalid role or unknown member.
    """
    if role not in VALID_MEMBER_ROLES:
        raise ValueError(
            f"Invalid role: {role}. Valid: {', '.join(sorted(VALID_MEMBER_ROLES))}"
        )
    members = data.get("members", [])
    if not isinstance(members, list):
        raise ValueError(f"Member not found: {member_id}")
    for m in members:
        if isinstance(m, dict) and m.get("id") == member_id:
            m["role"] = role
            return m
    raise ValueError(f"Member not found: {member_id}")


# ---------------------------------------------------------------------------
# Entry / Task operations
# ---------------------------------------------------------------------------

def task_add(data: dict, ms_id: str, description: str, *,
             entry_type: str = "task", date: str = "",
             detail: str = "", requested_by: str = "",
             priority: str = "", motivation: str = "",
             acceptance_criteria: str = "") -> str:
    """Add an entry to a milestone. Returns the new entry id."""
    target = find_target_milestone(data, ms_id)
    entries = target.setdefault("entries", [])
    eid = next_entry_id(data)
    meta = {}
    if requested_by:
        meta["requested_by"] = requested_by
    if priority:
        if priority not in VALID_PRIORITIES:
            raise ValueError(f"Invalid priority: {priority}. Valid: {', '.join(sorted(VALID_PRIORITIES))}")
        meta["priority"] = priority
    now = _now_iso()
    meta["created_by"] = _get_actor()
    entry = {
        "id": eid,
        "type": entry_type,
        "description": description,
        "date": date or now,
        "created_at": now,
        "done_at": None,
        "status": "todo",
        "meta": meta,
    }
    if detail:
        entry["detail"] = detail
    if motivation:
        entry["motivation"] = motivation
    if acceptance_criteria:
        entry["acceptance_criteria"] = acceptance_criteria
    entries.append(entry)
    return eid


def task_done(data: dict, entry_id: str, *, date: str = "", reason: str = "") -> tuple[dict, dict]:
    """Mark an entry as done. Returns (milestone, entry)."""
    result = find_entry(data, entry_id)
    if not result:
        raise ValueError(f"Entry not found: {entry_id}")
    ms, _, entry, _ = result
    entry["status"] = "done"
    entry["done_at"] = date or _now_iso()
    if not entry.get("date"):
        entry["date"] = entry["done_at"]
    meta = entry.setdefault("meta", {})
    meta["done_by"] = _get_actor()
    if reason:
        meta["done_reason"] = reason
    return ms, entry


def task_update(data: dict, entry_id: str, *,
                description: str = "", status: str = "",
                detail: str = "", date: str = "",
                motivation: str = "", acceptance_criteria: str = "",
                behavior: str = "", priority: str = "") -> tuple[dict, dict]:
    """Update entry fields. Returns (milestone, entry).

    The MS-32 "必要十分フォーマット" fields (motivation / acceptance_criteria /
    behavior) and priority are now updatable here. Empty strings are treated as
    "no change" so callers can omit fields they don't want to touch.
    """
    result = find_entry(data, entry_id)
    if not result:
        raise ValueError(f"Entry not found: {entry_id}")
    ms, _, entry, _ = result
    if description:
        entry["description"] = description
    if status:
        if status not in VALID_STATUSES:
            raise ValueError(
                f"Invalid status: {status}. Valid: {', '.join(sorted(VALID_STATUSES))}"
            )
        entry["status"] = status
        if status == "done" and not entry.get("done_at"):
            entry["done_at"] = date
    if detail:
        entry["detail"] = detail
    if motivation:
        entry["motivation"] = motivation
    if acceptance_criteria:
        entry["acceptance_criteria"] = acceptance_criteria
    if behavior:
        entry["behavior"] = behavior
    if priority:
        if priority not in VALID_PRIORITIES:
            raise ValueError(
                f"Invalid priority: {priority}. Valid: {', '.join(sorted(VALID_PRIORITIES))}"
            )
        meta = entry.setdefault("meta", {})
        meta["priority"] = priority
    return ms, entry


def task_delete(data: dict, entry_id: str, *, reason: str = "") -> dict:
    """Cancel an entry (soft delete). Returns the entry."""
    result = find_entry(data, entry_id)
    if not result:
        raise ValueError(f"Entry not found: {entry_id}")
    _, _, entry, _ = result
    entry["status"] = "cancelled"
    meta = entry.setdefault("meta", {})
    meta["cancelled_at"] = _now_iso()
    meta["cancelled_by"] = _get_actor()
    if reason:
        meta["cancel_reason"] = reason
    return entry


def entry_move(data: dict, entry_id: str, *,
               task_id: str = "", ms_id: str = "") -> None:
    """Move an entry under a task or to another milestone's top level."""
    if not task_id and not ms_id:
        raise ValueError("Must specify task_id or ms_id")

    src = find_entry(data, entry_id)
    if not src:
        raise ValueError(f"Entry not found: {entry_id}")
    _, src_list, entry, src_idx = src

    if ms_id:
        target_ms = None
        for ms in data["milestones"]:
            if ms["id"] == ms_id:
                target_ms = ms
                break
        if not target_ms:
            raise ValueError(f"Milestone not found: {ms_id}")
        src_list.pop(src_idx)
        target_ms.setdefault("entries", []).append(entry)
    else:
        dst = find_entry(data, task_id)
        if not dst:
            raise ValueError(f"Task not found: {task_id}")
        _, _, task_entry, _ = dst
        if task_entry.get("id") == entry_id:
            raise ValueError("Cannot move entry under itself")
        src_list.pop(src_idx)
        task_entry.setdefault("entries", []).append(entry)


# ---------------------------------------------------------------------------
# Progress & Summary
# ---------------------------------------------------------------------------

def update_progress(milestone: dict, progress_str: str) -> None:
    """Update milestone progress if specified. Auto-transitions status."""
    if progress_str:
        try:
            p = int(progress_str)
            milestone["progress"] = max(0, min(100, p))
        except ValueError:
            return

    p = milestone.get("progress", 0)
    if p > 0 and milestone.get("status") == "todo":
        milestone["status"] = "in_progress"


def auto_update_summary(data: dict, max_entries: int = 5) -> None:
    """Auto-update summary with recent work trail across all milestones."""
    all_entries = []
    for ms in data.get("milestones", []):
        all_entries.extend(_collect_entries_flat(ms.get("entries", [])))

    all_entries.sort(key=lambda x: (x[0], x[1]), reverse=True)
    recent = all_entries[:max_entries]

    if not recent:
        return

    recent.reverse()
    trail = " → ".join(desc for _, _, desc in recent)
    data["summary"] = f"直近の流れ: {trail}"


def _collect_entries_flat(entries: list) -> list:
    """Recursively collect all entries with timestamps into a flat list."""
    result = []
    for entry in entries:
        created = entry.get("created_at") or entry.get("date") or ""
        eid = entry.get("id", "e-0")
        try:
            id_num = int(eid.split("-", 1)[1]) if "-" in eid else 0
        except (ValueError, IndexError):
            id_num = 0
        if created:
            result.append((created, id_num, entry.get("description", "")))
        result.extend(_collect_entries_flat(entry.get("entries", [])))
    return result


# ---------------------------------------------------------------------------
# Commit logging
# ---------------------------------------------------------------------------

def check_duplicate_commit(entries: list, commit_hash: str) -> bool:
    """Check if commit hash already exists in entries (including nested)."""
    for entry in entries:
        if entry.get("type") == "commit" and entry.get("meta", {}).get("hash", "").startswith(commit_hash):
            return True
        if check_duplicate_commit(entry.get("entries", []), commit_hash):
            return True
    return False


def log_commit(data: dict, *, ms_id: str = "", commit_hash: str,
               message: str, date: str, summary: str = "",
               progress: str = "", behavior: str = "",
               resolves: str = "") -> dict:
    """Record a commit to the target milestone. Returns result info dict."""
    target = find_target_milestone(data, ms_id)
    entries = target.setdefault("entries", [])

    if check_duplicate_commit(entries, commit_hash):
        if progress:
            update_progress(target, progress)
        return {"status": "duplicate", "hash": commit_hash,
                "milestone": target["id"], "progress": target.get("progress", 0)}

    now = _now_iso()
    meta = {"hash": commit_hash, "message": message}
    if resolves:
        meta["resolves"] = resolves
    commit_entry = {
        "id": next_entry_id(data),
        "type": "commit",
        "description": summary or message,
        "date": date or now,
        "created_at": now,
        "done_at": now,
        "status": "done",
        "meta": meta,
    }
    if behavior:
        commit_entry["behavior"] = behavior

    commit_text = (summary or "") + " " + (message or "")
    matched_task = _find_matching_task(entries, commit_text)

    if matched_task:
        matched_task.setdefault("entries", []).append(commit_entry)
    else:
        entries.append(commit_entry)

    update_progress(target, progress)
    auto_update_summary(data)

    result = {
        "status": "logged",
        "hash": commit_hash,
        "entry_id": commit_entry["id"],
        "milestone": target["id"],
        "milestone_title": target.get("title", ""),
        "progress": target.get("progress", 0),
    }
    if matched_task:
        result["matched_task"] = matched_task["id"]
    return result


def _tokenize(text: str) -> set:
    """Split text into comparable tokens for Japanese and English."""
    en_words = re.findall(r'[a-zA-Z_]{2,}', text.lower())
    ja_text = re.sub(r'[a-zA-Z0-9_\s]+', ' ', text)
    ja_chunks = re.split(r'[のをにはがでとからまでもへや、。・\s]+', ja_text)
    ja_tokens = [c for c in ja_chunks if len(c) >= 2]
    return set(en_words + ja_tokens)


def _find_matching_task(entries: list, commit_text: str):
    """Find the best matching task for a commit based on content."""
    id_matches = re.findall(r'e-\d+', commit_text)
    if id_matches:
        for entry in entries:
            if entry.get("id") in id_matches and entry.get("type") == "task":
                return entry

    commit_tokens = _tokenize(commit_text)
    if not commit_tokens:
        return None

    candidates = []
    for entry in entries:
        if entry.get("type") != "task" or entry.get("status") in ("done", "cancelled"):
            continue
        task_text = entry.get("description", "") + " " + entry.get("detail", "")
        task_tokens = _tokenize(task_text)
        if not task_tokens:
            continue
        overlap = len(commit_tokens & task_tokens)
        if overlap > 0:
            score = overlap / min(len(commit_tokens), len(task_tokens))
            candidates.append((score, overlap, entry))

    if not candidates:
        return None

    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    best_score, best_overlap, best_entry = candidates[0]
    # Require at least 2 overlapping tokens or a score above 0.3 to avoid
    # false positives from coincidental single-word matches.
    if best_overlap >= 2 or (best_overlap >= 1 and best_score >= 0.3):
        return best_entry
    return None


# ---------------------------------------------------------------------------
# GitHub Issue import (ms-28)
# ---------------------------------------------------------------------------

def _find_imported_issue_numbers(data: dict) -> set:
    """Return set of GitHub issue numbers already imported as task entries."""
    imported = set()
    for ms in data.get("milestones", []):
        for entry in _iter_all_entries(ms.get("entries", [])):
            n = entry.get("meta", {}).get("issue_number")
            if n is not None:
                imported.add(int(n))
    return imported


def _iter_all_entries(entries: list):
    """Recursively yield all entries."""
    for entry in entries:
        yield entry
        yield from _iter_all_entries(entry.get("entries", []))


def issue_import(data: dict, *, ms_id: str = "", number: int, url: str,
                 title: str = "", body: str = "", date: str = "") -> str:
    """Import a GitHub Issue as a task entry. Returns the new entry id."""
    target = find_target_milestone(data, ms_id)
    entries = target.setdefault("entries", [])
    eid = next_entry_id(data)
    now = _now_iso()
    description = f"#{number}: {title}" if title else f"Issue #{number}"
    entry = {
        "id": eid,
        "type": "task",
        "description": description,
        "date": date or now,
        "created_at": now,
        "done_at": None,
        "status": "todo",
        "meta": {
            "issue_number": number,
            "issue_url": url,
            "created_by": _get_actor(),
        },
    }
    if body and body.strip():
        entry["detail"] = body.strip()[:500]
    entries.append(entry)
    return eid


# PR entry (ms-15)
# ---------------------------------------------------------------------------

def pr_add(data: dict, *, ms_id: str = "", url: str, author: str = "",
           intent: str = "", date: str = "", title: str = "",
           commits: list = None) -> str:
    """Add a PR entry to a milestone. Returns the new entry id.

    commits: list of {"oid": "<sha>", "messageHeadline": "<msg>"} from gh pr view.
    """
    import re as _re
    target = find_target_milestone(data, ms_id)
    entries = target.setdefault("entries", [])
    eid = next_entry_id(data)
    eid_num = int(eid.split("-")[1])

    pr_number = None
    m = _re.search(r'/pull/(\d+)', url)
    if m:
        pr_number = int(m.group(1))

    # Prefer explicit title; fall back to "PR#{n}" so URL stays only in meta
    description = title or (f"PR#{pr_number}" if pr_number else url)

    child_entries = []
    for i, commit in enumerate(commits or [], start=1):
        sha = commit.get("oid", "")
        msg = commit.get("messageHeadline", "") or (sha[:7] if sha else "commit")
        child_entries.append({
            "id": f"e-{eid_num + i}",
            "type": "commit",
            "description": msg,
            "date": date,
            "created_at": date,
            "status": "done",
            "meta": {"hash": sha[:7] if sha else ""},
        })

    entry = {
        "id": eid,
        "type": "pr",
        "description": description,
        "date": date,
        "created_at": date,
        "done_at": None,
        "status": "in_review",
        "meta": {
            "url": url,
            "author": author,
            "pr_number": pr_number,
            "pr_status": "in_review",
            "review_status": "pending",
            "intent": intent,
            "review_rationale": None,
        },
    }
    if child_entries:
        entry["entries"] = child_entries

    entries.append(entry)
    return eid


def _append_review_history(meta: dict, *, status: str,
                            rationale: str = "", actor: str = "") -> None:
    """Append a single transition to meta.review_history[] (e-609).

    Each entry: {"at": <iso>, "status": <new review_status>,
                 "rationale": <text>, "actor": <who>}

    This lives in meta so the timeline view can render the back-and-forth
    sequence: pending → changes_requested → pending → approved, etc.
    Idempotent only by *timestamp* — same status transitioned at the same
    millisecond will collide, but real usage has whole-second gaps.
    """
    history = meta.setdefault("review_history", [])
    if not isinstance(history, list):
        # Heal a corrupted field rather than crashing the caller.
        history = []
        meta["review_history"] = history
    history.append({
        "at": _now_iso(),
        "status": status,
        "rationale": rationale or "",
        "actor": actor or _get_actor(),
    })


def pr_request_review(data: dict, entry_id: str) -> tuple[dict, dict]:
    """Set PR to in_review. Returns (milestone, entry)."""
    result = find_entry(data, entry_id)
    if not result:
        raise ValueError(f"Entry not found: {entry_id}")
    ms, _, entry, _ = result
    if entry.get("type") != "pr":
        raise ValueError(f"Entry {entry_id} is not a pr entry")
    meta = entry.setdefault("meta", {})
    meta["pr_status"] = "in_review"
    meta["review_status"] = "pending"
    meta["review_requested_at"] = _now_iso()
    entry["status"] = "in_review"
    _append_review_history(meta, status="pending")
    return ms, entry


def pr_request_changes(data: dict, entry_id: str, *, rationale: str = "") -> tuple[dict, dict]:
    """Request changes on a PR: review_status=changes_requested, entry stays in_review."""
    result = find_entry(data, entry_id)
    if not result:
        raise ValueError(f"Entry not found: {entry_id}")
    ms, _, entry, _ = result
    if entry.get("type") != "pr":
        raise ValueError(f"Entry {entry_id} is not a pr entry")
    meta = entry.setdefault("meta", {})
    meta["pr_status"] = "in_review"
    meta["review_status"] = "changes_requested"
    meta["reviewed_at"] = _now_iso()
    entry["status"] = "in_review"
    if rationale:
        meta["review_rationale"] = rationale
    _append_review_history(meta, status="changes_requested", rationale=rationale)
    return ms, entry


def pr_record_review(data: dict, entry_id: str, *, review_text: str,
                     verdict: str, date: str = "") -> tuple[dict, dict, dict]:
    """Record an AI code review as a child note entry under the PR.

    verdict: 'approved' or 'changes_requested'
    Returns (milestone, pr_entry, note_entry).
    """
    import datetime as _dt
    result = find_entry(data, entry_id)
    if not result:
        raise ValueError(f"Entry not found: {entry_id}")
    ms, _, entry, _ = result
    if entry.get("type") != "pr":
        raise ValueError(f"Entry {entry_id} is not a pr entry")

    now = _now_iso()
    note_eid = next_entry_id(data)
    note_entry = {
        "id": note_eid,
        "type": "note",
        "description": f"[AI Review] {verdict}",
        "detail": review_text,
        "date": date or now,
        "created_at": now,
        "done_at": now,
        "status": "done",
    }
    entry.setdefault("entries", []).append(note_entry)

    meta = entry.setdefault("meta", {})
    meta["review_status"] = verdict
    if verdict == "approved":
        meta["pr_status"] = "approved"
        entry["status"] = "approved"
    else:
        meta["pr_status"] = "in_review"
        entry["status"] = "in_review"
    _append_review_history(meta, status=verdict, rationale=review_text[:200])

    return ms, entry, note_entry


def pr_merge(data: dict, entry_id: str, *, date: str = "") -> tuple[dict, dict]:
    """Merge a PR: pr_status=merged, entry.status=done, done_at=today.

    Side effect (e-610): for each commit hash recorded under the PR's child
    entries, find any *other* commit entry across all milestones with the
    same short hash and tag it with `meta.pr_id = entry_id`. This is the
    "the same commit shows up both as a beacon-log entry and as a PR child
    entry — link them so the timeline shows the PR origin" rule. Idempotent:
    re-merging the same PR re-applies the same tag.
    """
    import datetime as _dt
    result = find_entry(data, entry_id)
    if not result:
        raise ValueError(f"Entry not found: {entry_id}")
    ms, _, entry, _ = result
    if entry.get("type") != "pr":
        raise ValueError(f"Entry {entry_id} is not a pr entry")
    meta = entry.setdefault("meta", {})
    now = _now_iso()
    meta["pr_status"] = "merged"
    meta["merged_at"] = now
    entry["status"] = "done"
    entry["done_at"] = date or now

    # e-610: back-link beacon-side commit entries to this PR.
    pr_commit_hashes = set()
    for child in entry.get("entries", []) or []:
        if isinstance(child, dict) and child.get("type") == "commit":
            h = (child.get("meta") or {}).get("hash", "")
            if h:
                pr_commit_hashes.add(h[:7])

    if pr_commit_hashes:
        linked = 0
        for ms_iter in data.get("milestones", []):
            if not isinstance(ms_iter, dict):
                continue
            for ent in ms_iter.get("entries", []) or []:
                if not isinstance(ent, dict) or ent.get("type") != "commit":
                    continue
                # Skip the PR's own child entries — they already live under
                # the PR; we don't need a self-loop link.
                if any(ent is c for c in entry.get("entries", []) or []):
                    continue
                ent_meta = ent.get("meta") or {}
                ent_hash = (ent_meta.get("hash") or "")[:7]
                if ent_hash and ent_hash in pr_commit_hashes:
                    ent_meta = ent.setdefault("meta", {})
                    ent_meta["pr_id"] = entry_id
                    linked += 1
        # Record how many backlinks we just stamped (informational).
        meta["linked_commits"] = linked

    return ms, entry


def pr_close(data: dict, entry_id: str) -> tuple[dict, dict]:
    """Close a PR without merging: pr_status=closed, entry.status=cancelled."""
    result = find_entry(data, entry_id)
    if not result:
        raise ValueError(f"Entry not found: {entry_id}")
    ms, _, entry, _ = result
    if entry.get("type") != "pr":
        raise ValueError(f"Entry {entry_id} is not a pr entry")
    meta = entry.setdefault("meta", {})
    meta["pr_status"] = "closed"
    meta["closed_at"] = _now_iso()
    entry["status"] = "cancelled"
    return ms, entry


def pr_approve(data: dict, entry_id: str, *, rationale: str = "") -> tuple[dict, dict]:
    """Approve a PR: pr_status=approved, review_status=approved, entry.status=approved."""
    result = find_entry(data, entry_id)
    if not result:
        raise ValueError(f"Entry not found: {entry_id}")
    ms, _, entry, _ = result
    if entry.get("type") != "pr":
        raise ValueError(f"Entry {entry_id} is not a pr entry")
    meta = entry.setdefault("meta", {})
    meta["pr_status"] = "approved"
    meta["review_status"] = "approved"
    meta["reviewed_at"] = _now_iso()
    entry["status"] = "approved"
    if rationale:
        meta["review_rationale"] = rationale
    _append_review_history(meta, status="approved", rationale=rationale)
    return ms, entry


def pr_reject(data: dict, entry_id: str, *, rationale: str = "") -> tuple[dict, dict]:
    """Reject a PR: review_status=rejected, entry.status=cancelled."""
    result = find_entry(data, entry_id)
    if not result:
        raise ValueError(f"Entry not found: {entry_id}")
    ms, _, entry, _ = result
    if entry.get("type") != "pr":
        raise ValueError(f"Entry {entry_id} is not a pr entry")
    meta = entry.setdefault("meta", {})
    meta["pr_status"] = "closed"
    meta["review_status"] = "rejected"
    meta["reviewed_at"] = _now_iso()
    meta["closed_at"] = _now_iso()
    entry["status"] = "cancelled"
    if rationale:
        meta["review_rationale"] = rationale
    _append_review_history(meta, status="rejected", rationale=rationale)
    return ms, entry


# ---------------------------------------------------------------------------
# Save entry (ms-16)
# ---------------------------------------------------------------------------

def check_duplicate_save(entries: list, source: str, url: str, revision_id: str) -> bool:
    """Check if a save entry with same source+identifier already exists."""
    if source == "manual":
        return False
    for entry in entries:
        if entry.get("type") == "save":
            meta = entry.get("meta", {})
            if meta.get("source") == source:
                if url and meta.get("url") == url:
                    return True
                if revision_id and meta.get("revision_id") == revision_id:
                    return True
        if check_duplicate_save(entry.get("entries", []), source, url, revision_id):
            return True
    return False


def save_entry(data: dict, *, ms_id: str = "", description: str,
               source: str, date: str, url: str = "",
               revision_id: str = "", hash: str = "",
               progress: str = "") -> dict:
    """Record a save entry to the target milestone. Returns result info dict."""
    target = find_target_milestone(data, ms_id)
    entries = target.setdefault("entries", [])

    if check_duplicate_save(entries, source, url, revision_id):
        if progress:
            update_progress(target, progress)
        return {"status": "duplicate", "milestone": target["id"],
                "progress": target.get("progress", 0)}

    now = _now_iso()
    entry = {
        "id": next_entry_id(data),
        "type": "save",
        "description": description,
        "date": date or now,
        "created_at": now,
        "done_at": now,
        "status": "done",
        "meta": {"source": source},
    }
    if url:
        entry["meta"]["url"] = url
    if revision_id:
        entry["meta"]["revision_id"] = revision_id
    if hash:
        entry["meta"]["hash"] = hash
    entries.append(entry)
    update_progress(target, progress)

    return {"status": "saved", "entry_id": entry["id"],
            "milestone": target["id"], "progress": target.get("progress", 0)}


# ---------------------------------------------------------------------------
# Multi-agent coordination (ms-17)
# ---------------------------------------------------------------------------

def milestone_depends(data: dict, ms_id: str, depends_on: list) -> dict:
    """Set depends_on for a milestone. Returns the milestone."""
    all_ids = {ms["id"] for ms in data["milestones"]}
    for dep_id in depends_on:
        if dep_id == ms_id:
            raise ValueError("Milestone cannot depend on itself")
        if dep_id not in all_ids:
            raise ValueError(f"Dependency not found: {dep_id}")
    for ms in data["milestones"]:
        if ms["id"] == ms_id:
            if depends_on:
                ms["depends_on"] = depends_on
            elif "depends_on" in ms:
                del ms["depends_on"]
            return ms
    raise ValueError(f"Milestone not found: {ms_id}")


def milestone_workspace(data: dict, ms_id: str, workspace: str) -> dict:
    """Set workspace path for a milestone. Returns the milestone."""
    for ms in data["milestones"]:
        if ms["id"] == ms_id:
            if workspace:
                ms["workspace"] = workspace
            elif "workspace" in ms:
                del ms["workspace"]
            return ms
    raise ValueError(f"Milestone not found: {ms_id}")


# OSS: worktree lifecycle management core
# The functions below handle git worktree fields on milestone objects.
# workspace_branch, workspace_path, executor, executor_assigned_at are optional fields.
# Human Executor notification (trigger fire, multi-user management) is closed-source.

def milestone_worktree_set(
    data: dict,
    ms_id: str,
    workspace_branch: str,
    workspace_path: str,
    executor: str,
    executor_assigned_at: str,
) -> dict:
    """
    Set worktree fields on a milestone (OSS: git worktree lifecycle core).
    All fields are optional; pass empty string to skip setting.
    Returns the updated milestone dict.
    """
    for ms in data["milestones"]:
        if ms["id"] == ms_id:
            if workspace_branch:
                ms["workspace_branch"] = workspace_branch
            if workspace_path:
                ms["workspace_path"] = workspace_path
            if executor:
                ms["executor"] = executor
            if executor_assigned_at:
                ms["executor_assigned_at"] = executor_assigned_at
            return ms
    raise ValueError(f"Milestone not found: {ms_id}")


def milestone_worktree_clear(data: dict, ms_id: str) -> dict:
    """
    Clear worktree fields from a milestone after cleanup (OSS: git worktree lifecycle core).
    Returns the updated milestone dict.
    """
    for ms in data["milestones"]:
        if ms["id"] == ms_id:
            for field in ("workspace_path", "workspace_branch", "executor", "executor_assigned_at"):
                ms.pop(field, None)
            return ms
    raise ValueError(f"Milestone not found: {ms_id}")


def milestone_graph(data: dict) -> dict:
    """Build dependency graph with topological waves. Returns {nodes, edges, waves}."""
    nodes = []
    edges = []
    id_set = set()

    for ms in data["milestones"]:
        if ms.get("status") == "cancelled":
            continue
        ms_id = ms["id"]
        id_set.add(ms_id)
        nodes.append({
            "id": ms_id,
            "title": ms.get("title", ""),
            "status": ms.get("status", ""),
            "progress": ms.get("progress", 0),
            "workspace": ms.get("workspace"),
            "depends_on": ms.get("depends_on", []),
        })
        for dep in ms.get("depends_on", []):
            edges.append({"from": ms_id, "to": dep, "type": "depends_on"})

    # Cross-MS tasks
    for ms in data["milestones"]:
        if ms.get("status") == "cancelled":
            continue
        for entry in ms.get("entries", []):
            req_by = entry.get("meta", {}).get("requested_by", "")
            if req_by and req_by in id_set:
                edges.append({"from": req_by, "to": ms["id"],
                              "type": "cross_task", "task_id": entry.get("id", "")})

    # Kahn's algorithm for waves
    in_degree = {n["id"]: 0 for n in nodes}
    for n in nodes:
        for dep in n["depends_on"]:
            if dep in in_degree:
                in_degree[n["id"]] += 1

    waves = []
    remaining = dict(in_degree)
    while remaining:
        wave = sorted([nid for nid, deg in remaining.items() if deg == 0])
        if not wave:
            # Cycle detected - dump remaining as final wave with warning
            wave = sorted(remaining.keys())
            waves.append({"wave": len(waves) + 1, "milestones": wave, "cycle": True})
            break
        waves.append({"wave": len(waves) + 1, "milestones": wave})
        for nid in wave:
            del remaining[nid]
        for n in nodes:
            if n["id"] in remaining:
                for dep in n["depends_on"]:
                    if dep in {w for w in wave}:
                        remaining[n["id"]] -= 1

    return {"nodes": nodes, "edges": edges, "waves": waves}


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def entries_to_json(entries: list) -> list:
    """Convert entries to a JSON-serializable list (recursive)."""
    result = []
    for e in entries:
        item = {
            "id": e.get("id", ""),
            "type": e.get("type", ""),
            "description": e.get("description", ""),
            "status": e.get("status", "todo"),
            "created_at": e.get("created_at", e.get("date", "")),
            "done_at": e.get("done_at"),
        }
        if e.get("meta"):
            item["meta"] = e.get("meta", {})
        if e.get("detail"):
            item["detail"] = e["detail"]
        for field in ("motivation", "acceptance_criteria", "behavior"):
            if e.get(field):
                item[field] = e[field]
        children = e.get("entries", [])
        if children:
            item["entries"] = entries_to_json(children)
        result.append(item)
    return result


def count_task_status(entries: list) -> tuple[int, int]:
    """Count total and done tasks/commits/PRs recursively. Returns (total, done)."""
    total = 0
    done = 0
    for e in entries:
        if e.get("type") in ("task", "commit", "pr"):
            total += 1
            if e.get("status") in ("done", "cancelled"):
                done += 1
        t, d = count_task_status(e.get("entries", []))
        total += t
        done += d
    return total, done


def filter_cancelled(entries: list, show_all: bool = False) -> list:
    """Filter out cancelled entries recursively."""
    if show_all:
        return entries
    result = []
    for e in entries:
        if e.get("status") == "cancelled":
            continue
        filtered = dict(e)
        children = e.get("entries", [])
        if children:
            filtered["entries"] = filter_cancelled(children, show_all)
        result.append(filtered)
    return result


def milestone_prepare_info(ms: dict) -> dict:
    """Build prepare info dict for a single milestone (used by log --prepare)."""
    entries = ms.get("entries", [])
    total_tasks, done_tasks = count_task_status(entries)

    all_flat = _collect_entries_flat(entries)
    all_flat.sort(key=lambda x: (x[0], x[1]), reverse=True)
    recent = [{"date": d, "description": desc} for d, _, desc in all_flat[:5]]

    pending_tasks = []
    for e in entries:
        if e.get("type") == "task" and e.get("status") not in ("done", "cancelled"):
            pending_tasks.append({"id": e["id"], "description": e.get("description", "")})

    return {
        "id": ms["id"],
        "title": ms.get("title", ""),
        "status": ms.get("status", ""),
        "progress": ms.get("progress", 0),
        "total_tasks": total_tasks,
        "done_tasks": done_tasks,
        "pending_tasks": pending_tasks,
        "recent_entries": recent,
    }


def collect_retro_entries(entries: list, since: str, until: str) -> list:
    """Recursively collect entries within date range for retrospective."""
    result = []
    for entry in entries:
        date = entry.get("created_at") or entry.get("date") or ""
        status = entry.get("status", "")
        if status == "cancelled":
            continue
        in_range = True
        if since and date < since:
            in_range = False
        if until and date > until:
            in_range = False

        children = entry.get("entries", [])
        child_results = collect_retro_entries(children, since, until) if children else []

        if in_range or child_results:
            item = {
                "id": entry.get("id", ""),
                "type": entry.get("type", ""),
                "description": entry.get("description", ""),
                "status": status,
                "date": date,
            }
            if entry.get("detail"):
                item["detail"] = entry["detail"]
            if entry.get("meta"):
                item["meta"] = entry["meta"]
            if entry.get("done_at"):
                item["done_at"] = entry["done_at"]
            if child_results:
                item["entries"] = child_results
            if in_range:
                result.append(item)

    return result



# ---------------------------------------------------------------------------
# Deploy operations
# ---------------------------------------------------------------------------

def deploy_void(data: dict, deploy_id: str, *, reason: str) -> dict:
    """Mark a deployment record as voided (immutable — never physically deleted).

    Returns the updated deployment entry.
    """
    for dep in data.get("deployments", []):
        if dep.get("id") == deploy_id:
            dep["voided"] = True
            dep["void_reason"] = reason
            dep["voided_at"] = _now_iso()
            dep["voided_by"] = _get_actor()
            return dep
    raise ValueError(f"Deployment not found: {deploy_id}")


# ---------------------------------------------------------------------------
# Operation operations
# ---------------------------------------------------------------------------

def _find_operation(data: dict, op_id: str) -> dict:
    for op in data.get("operations", []):
        if op.get("id") == op_id:
            return op
    raise ValueError(f"Operation not found: {op_id}")


def operation_open(data: dict, title: str, *,
                   schedule: str = "weekdays", log_source: str = "",
                   status: str = "open", activation_hint: str = "",
                   objective: str = "", acceptance_criteria: str = "",
                   priority: str = "") -> tuple[dict, dict]:
    """Create a new Operation. Returns (data, operation).

    Defaults to status='open' for backward compat. Pass status='todo' to
    create an outline-only Operation that will be activated later via
    operation_set_status (todo → in_progress → open).
    """
    if schedule not in SCHEDULE_DAYS:
        raise ValueError(f"Invalid schedule: {schedule}. Valid: {', '.join(sorted(SCHEDULE_DAYS))}")
    if status not in VALID_OP_STATUSES:
        raise ValueError(f"Invalid status: {status}. Valid: {', '.join(sorted(VALID_OP_STATUSES))}")
    if priority and priority not in VALID_PRIORITIES:
        raise ValueError(f"Invalid priority: {priority}. Valid: {', '.join(sorted(VALID_PRIORITIES))}")
    op_id = next_op_id(data)
    op = {
        "id": op_id,
        "title": title,
        "status": status,
        "opened_at": _now_iso() if status == "open" else None,
        "closed_at": None,
        "schedule": {
            "frequency": schedule,
            "days": SCHEDULE_DAYS[schedule],
        },
        "log_source": log_source or op_id,
        "entries": [],
    }
    if activation_hint:
        op["activation_hint"] = activation_hint
    if objective:
        op["objective"] = objective
    if acceptance_criteria:
        op["acceptance_criteria"] = acceptance_criteria
    if priority:
        op["priority"] = priority
    data.setdefault("operations", []).append(op)
    return data, op


def operation_set_status(data: dict, op_id: str, status: str) -> dict:
    """Transition an Operation's status. Records timestamp for open transitions."""
    if status not in VALID_OP_STATUSES:
        raise ValueError(f"Invalid status: {status}. Valid: {', '.join(sorted(VALID_OP_STATUSES))}")
    op = _find_operation(data, op_id)
    prev = op["status"]
    op["status"] = status
    if status == "open" and not op.get("opened_at"):
        op["opened_at"] = _now_iso()
    if status == "closed":
        op["closed_at"] = _now_iso()
    op.setdefault("meta", {})[f"{status}_at"] = _now_iso()
    op.setdefault("meta", {})[f"{status}_by"] = _get_actor()
    return op


def operation_update(data: dict, op_id: str, *,
                     title: str = "", schedule: str = "",
                     activation_hint: str = "", objective: str = "",
                     acceptance_criteria: str = "", priority: str = "",
                     log_source: str = "") -> dict:
    """Update Operation metadata fields."""
    op = _find_operation(data, op_id)
    if title:
        op["title"] = title
    if schedule:
        if schedule not in SCHEDULE_DAYS:
            raise ValueError(f"Invalid schedule: {schedule}. Valid: {', '.join(sorted(SCHEDULE_DAYS))}")
        op["schedule"] = {"frequency": schedule, "days": SCHEDULE_DAYS[schedule]}
    if activation_hint:
        op["activation_hint"] = activation_hint
    if objective:
        op["objective"] = objective
    if acceptance_criteria:
        op["acceptance_criteria"] = acceptance_criteria
    if priority:
        if priority not in VALID_PRIORITIES:
            raise ValueError(f"Invalid priority: {priority}. Valid: {', '.join(sorted(VALID_PRIORITIES))}")
        op["priority"] = priority
    if log_source:
        op["log_source"] = log_source
    return op


def operation_close(data: dict, op_id: str) -> dict:
    """Close an Operation. Returns the operation."""
    op = _find_operation(data, op_id)
    if op["status"] == "closed":
        raise ValueError(f"Operation {op_id} is already closed")
    op["status"] = "closed"
    op["closed_at"] = _now_iso()
    return op


def operation_task_add(data: dict, op_id: str, description: str, *,
                       priority: str = "", motivation: str = "",
                       acceptance_criteria: str = "") -> tuple[dict, dict]:
    """Add an operation_task entry to an Operation. Returns (operation, entry)."""
    op = _find_operation(data, op_id)
    if priority and priority not in VALID_PRIORITIES:
        raise ValueError(f"Invalid priority: {priority}. Valid: {', '.join(sorted(VALID_PRIORITIES))}")
    eid = next_entry_id(data)
    entry = {
        "id": eid,
        "type": "operation_task",
        "description": description,
        "status": "todo",
        "created_at": _now_iso(),
        "done_at": None,
        "meta": {"created_by": _get_actor()},
    }
    if priority:
        entry["meta"]["priority"] = priority
    if motivation:
        entry["motivation"] = motivation
    if acceptance_criteria:
        entry["acceptance_criteria"] = acceptance_criteria
    op.setdefault("entries", []).append(entry)
    return op, entry


def operation_task_done(data: dict, entry_id: str, *, reason: str = "") -> dict:
    """Mark an operation_task as done. Returns the entry."""
    for op in data.get("operations", []):
        for e in op.get("entries", []):
            if e.get("id") == entry_id and e.get("type") == "operation_task":
                e["status"] = "done"
                e["done_at"] = _now_iso()
                if reason:
                    e["done_reason"] = reason
                return e
    raise ValueError(f"operation_task not found: {entry_id}")


def run_record_add(data: dict, op_id: str, *,
                   batch: str, status: str, description: str,
                   date: str = "") -> tuple[dict, dict]:
    """Add a run_record entry to an Operation. Returns (operation, entry)."""
    if status not in VALID_RUN_STATUSES:
        raise ValueError(f"Invalid status: {status}. Valid: ok, warning, error")
    op = _find_operation(data, op_id)
    eid = next_entry_id(data)
    entry = {
        "id": eid,
        "type": "run_record",
        "batch": batch,
        "status": status,
        "description": description,
        "date": date or _now_iso(),
        "meta": {"created_by": _get_actor()},
    }
    op.setdefault("entries", []).append(entry)
    return op, entry


def incident_open(data: dict, op_id: str, *,
                  title: str, description: str = "",
                  priority: str = "") -> tuple[dict, dict]:
    """Open an Incident in an Operation. Returns (operation, entry)."""
    op = _find_operation(data, op_id)
    eid = next_entry_id(data)
    if priority and priority not in VALID_PRIORITIES:
        raise ValueError(f"Invalid priority: {priority}. Valid: {', '.join(sorted(VALID_PRIORITIES))}")
    entry = {
        "id": eid,
        "type": "incident",
        "title": title,
        "status": "open",
        "description": description,
        "opened_at": _now_iso(),
        "resolved_at": None,
        "resolution": None,
        "linked_ms_task": None,
        "meta": {"created_by": _get_actor()},
    }
    if priority:
        entry["priority"] = priority
    op.setdefault("entries", []).append(entry)
    return op, entry


def incident_close(data: dict, incident_id: str, *, resolution: str) -> tuple[dict, dict]:
    """Resolve an Incident. Returns (operation, entry)."""
    result = find_entry(data, incident_id)
    if not result:
        raise ValueError(f"Incident not found: {incident_id}")
    container, _, entry, _ = result
    if entry.get("type") != "incident":
        raise ValueError(f"{incident_id} is not an incident entry")
    entry["status"] = "resolved"
    entry["resolved_at"] = _now_iso()
    entry["resolution"] = resolution
    meta = entry.setdefault("meta", {})
    meta["resolved_by"] = _get_actor()
    return container, entry


def incident_escalate(data: dict, incident_id: str, ms_id: str) -> tuple[dict, dict, dict]:
    """Escalate an Incident to a Milestone task. Returns (operation, incident_entry, task_entry)."""
    result = find_entry(data, incident_id)
    if not result:
        raise ValueError(f"Incident not found: {incident_id}")
    op, _, incident, _ = result
    if incident.get("type") != "incident":
        raise ValueError(f"{incident_id} is not an incident entry")
    ms = find_target_milestone(data, ms_id)
    eid = next_entry_id(data)
    task = {
        "id": eid,
        "type": "task",
        "description": f"[Incident] {incident.get('title', incident_id)}: {incident.get('description', '')}".strip(": "),
        "status": "todo",
        "created_at": _now_iso(),
        "meta": {
            "created_by": _get_actor(),
            "escalated_from": incident_id,
            "escalated_from_op": op.get("id"),
        },
    }
    ms.setdefault("entries", []).append(task)
    incident["linked_ms_task"] = eid
    return op, incident, task
