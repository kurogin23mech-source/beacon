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
VALID_ENTRY_TYPES = {"commit", "task", "note", "save", "pr"}
# PR lifecycle: in_review → approved → merged (or closed/rejected)
# "open" is reserved for Phase 2 auto-detection via GitHub API (external PRs not yet picked up by beacon)
VALID_PR_STATUSES = {"open", "in_review", "approved", "merged", "closed"}
VALID_REVIEW_STATUSES = {"pending", "approved", "changes_requested", "rejected"}
MS_ID_RE = re.compile(r"^ms-\d+$")
ENTRY_ID_RE = re.compile(r"^e-\d+$")


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
    """Generate next entry id across all milestones (including nested)."""
    max_id = 0
    for ms in data["milestones"]:
        for entry in ms.get("entries", []):
            max_id = _max_entry_id(entry, max_id)
    return f"e-{max_id + 1}"


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
    """Find an entry by ID across all milestones (including nested).

    Returns (milestone, parent_entries_list, entry, index) or None.
    """
    for ms in data["milestones"]:
        result = _find_entry_in(ms.get("entries", []), entry_id, ms)
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
                   description: str = "") -> str:
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
                     description: str = "", reason: str = "") -> dict:
    """Update milestone fields. Returns the milestone."""
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
# Entry / Task operations
# ---------------------------------------------------------------------------

def task_add(data: dict, ms_id: str, description: str, *,
             entry_type: str = "task", date: str = "",
             detail: str = "", requested_by: str = "") -> str:
    """Add an entry to a milestone. Returns the new entry id."""
    target = find_target_milestone(data, ms_id)
    entries = target.setdefault("entries", [])
    eid = next_entry_id(data)
    meta = {}
    if requested_by:
        meta["requested_by"] = requested_by
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
                detail: str = "", date: str = "") -> tuple[dict, dict]:
    """Update entry fields. Returns (milestone, entry)."""
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
               progress: str = "") -> dict:
    """Record a commit to the target milestone. Returns result info dict."""
    target = find_target_milestone(data, ms_id)
    entries = target.setdefault("entries", [])

    if check_duplicate_commit(entries, commit_hash):
        if progress:
            update_progress(target, progress)
        return {"status": "duplicate", "hash": commit_hash,
                "milestone": target["id"], "progress": target.get("progress", 0)}

    now = _now_iso()
    commit_entry = {
        "id": next_entry_id(data),
        "type": "commit",
        "description": summary or message,
        "date": date or now,
        "created_at": now,
        "done_at": now,
        "status": "done",
        "meta": {"hash": commit_hash, "message": message},
    }

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
        active_tasks = [e for e in entries if e.get("type") == "task" and e.get("status") not in ("done", "cancelled")]
        if len(active_tasks) == 1:
            return active_tasks[0]
        return None

    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    best_score, best_overlap, best_entry = candidates[0]
    if best_overlap >= 1:
        return best_entry
    return None


# ---------------------------------------------------------------------------
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

    return ms, entry, note_entry


def pr_merge(data: dict, entry_id: str, *, date: str = "") -> tuple[dict, dict]:
    """Merge a PR: pr_status=merged, entry.status=done, done_at=today."""
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
