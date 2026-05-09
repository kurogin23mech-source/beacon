#!/usr/bin/env python3
"""Beacon CLI commands - called from beacon shell script with env vars."""

import json
import os
import re
import subprocess
import sys


VALID_STATUSES = {"todo", "in_progress", "in_review", "waiting", "done", "cancelled"}
VALID_ENTRY_TYPES = {"commit", "task", "note"}
MS_ID_RE = re.compile(r"^ms-\d+$")
ENTRY_ID_RE = re.compile(r"^e-\d+$")


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


def _validate_entry(entry, ms_id):
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
  - マイルストーン追加: `beacon milestone add "タイトル" [-d 目標日]`
  - タスク追加: `beacon task add "説明" -m <ms-id>`
  - タスク完了: `beacon task done <entry-id>`
  - 進捗記録: `beacon log "概要"`
  - 同期: `beacon sync`
- 実装を開始する前に、必ず `.beacon/project.json` のマイルストーン一覧を確認し、「この作業はどのマイルストーンに向かうものか」をユーザーに確認すること
- コミット後は `beacon log "概要" -p <進捗率>` でアクティブマイルストーンに記録する
  - 進捗率は必ず付ける。タスク消化率ではなく、マイルストーンの目標に対してどれくらい進んだかを定性的に評価して0-100で指定する
- 同じ課題に2回以上コミットが発生したら、タスクにまとめることを提案する
- マイルストーンの追加・完了は `beacon milestone` コマンドで管理する
- セッション終了時や方向性が変わった時は `beacon summary "テキスト"` で現在地を更新する
  - タスクリストを見ればわかる情報は書かない（進捗率やアクティブMS名など）
  - 書くべきこと: なぜ今のタスクに取り組んでいるのか、どういう経緯でこうなったか、次セッションで知っておくべき背景や判断
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
            total_tasks, done_tasks = _count_task_status_flat(entries)
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

    icons = {"done": "\u25cf", "in_progress": "\u25d0", "todo": "\u25cb",
             "waiting": "\u25cc", "in_review": "\u25d1", "cancelled": "\u2718"}
    for ms in milestones:
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


def cmd_milestone_show():
    ms_id = os.environ.get("BEACON_MS_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()

    for ms in data["milestones"]:
        if ms["id"] == ms_id:
            entries = ms.get("entries", [])
            total_tasks, done_tasks = _count_task_status_flat(entries)
            if json_mode:
                output = {
                    "id": ms["id"],
                    "title": ms.get("title", ""),
                    "status": ms.get("status", "todo"),
                    "progress": ms.get("progress", 0),
                    "target_date": ms.get("target_date", ""),
                    "total_tasks": total_tasks,
                    "done_tasks": done_tasks,
                    "entries": _entries_to_json(entries),
                }
                print(json.dumps(output, ensure_ascii=False))
            else:
                icons = {"done": "\u25cf", "in_progress": "\u25d0", "todo": "\u25cb",
                         "waiting": "\u25cc", "in_review": "\u25d1", "cancelled": "\u2718"}
                icon = icons.get(ms["status"], "?")
                print(f"{icon} [{ms['id']}] {ms['title']}")
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

    for ms in data["milestones"]:
        if ms["id"] == ms_id:
            title = os.environ.get("BEACON_TITLE", "")
            progress = os.environ.get("BEACON_PROGRESS", "")
            target_date = os.environ.get("BEACON_TARGET_DATE", "")
            status = os.environ.get("BEACON_STATUS", "")

            if title:
                ms["title"] = title
            if progress:
                try:
                    ms["progress"] = max(0, min(100, int(progress)))
                except ValueError:
                    pass
            if target_date:
                ms["target_date"] = target_date
            if status:
                if status not in VALID_STATUSES:
                    print(f"Invalid status: {status}. Valid: {', '.join(sorted(VALID_STATUSES))}")
                    sys.exit(1)
                ms["status"] = status

            save_project(data)
            if json_mode:
                print(json.dumps({"id": ms["id"], "title": ms["title"],
                                  "status": ms["status"], "progress": ms.get("progress", 0)},
                                 ensure_ascii=False))
            else:
                print(f"Updated: [{ms['id']}] {ms['title']}")
            return

    print(f"Milestone not found: {ms_id}")
    sys.exit(1)


def cmd_milestone_delete():
    ms_id = os.environ.get("BEACON_MS_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()

    for ms in data["milestones"]:
        if ms["id"] == ms_id:
            ms["status"] = "cancelled"
            save_project(data)
            if json_mode:
                print(json.dumps({"id": ms["id"], "status": "cancelled"}, ensure_ascii=False))
            else:
                print(f"Cancelled: [{ms['id']}] {ms['title']}")
            return

    print(f"Milestone not found: {ms_id}")
    sys.exit(1)


def _entries_to_json(entries):
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
        if e.get("type") == "commit":
            item["meta"] = e.get("meta", {})
        if e.get("detail"):
            item["detail"] = e["detail"]
        children = e.get("entries", [])
        if children:
            item["entries"] = _entries_to_json(children)
        result.append(item)
    return result


def _count_task_status_flat(entries):
    """Count total and done tasks recursively."""
    total = 0
    done = 0
    for e in entries:
        if e.get("type") == "task":
            total += 1
            if e.get("status") == "done":
                done += 1
        t, d = _count_task_status_flat(e.get("entries", []))
        total += t
        done += d
    return total, done


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


def _collect_entries_flat(entries):
    """Recursively collect all entries with timestamps into a flat list."""
    result = []
    for entry in entries:
        created = entry.get("created_at") or entry.get("date") or ""
        eid = entry.get("id", "e-0")
        # Extract numeric part of ID for sorting (e-1 → 1, e-22 → 22)
        try:
            id_num = int(eid.split("-", 1)[1]) if "-" in eid else 0
        except (ValueError, IndexError):
            id_num = 0
        if created:
            result.append((created, id_num, entry.get("description", "")))
        result.extend(_collect_entries_flat(entry.get("entries", [])))
    return result


def auto_update_summary(data, max_entries=5):
    """Auto-update summary with recent work trail across all milestones."""
    all_entries = []
    for ms in data.get("milestones", []):
        all_entries.extend(_collect_entries_flat(ms.get("entries", [])))

    # Sort by (date, entry_id) descending, take recent N
    all_entries.sort(key=lambda x: (x[0], x[1]), reverse=True)
    recent = all_entries[:max_entries]

    if not recent:
        return

    # Build trail in chronological order (oldest → newest)
    recent.reverse()
    trail = " → ".join(desc for _, _, desc in recent)
    data["summary"] = f"直近の流れ: {trail}"


def _tokenize(text):
    """Split text into comparable tokens for Japanese and English."""
    import re
    # English words (2+ chars)
    en_words = re.findall(r'[a-zA-Z_]{2,}', text.lower())
    # Japanese: split on particles/punctuation, keep chunks of 2+ chars
    ja_text = re.sub(r'[a-zA-Z0-9_\s]+', ' ', text)
    ja_chunks = re.split(r'[のをにはがでとからまでもへや、。・\s]+', ja_text)
    ja_tokens = [c for c in ja_chunks if len(c) >= 2]
    return set(en_words + ja_tokens)


def _find_matching_task(entries, commit_text):
    """Find the best matching task for a commit based on content.
    Returns the task entry or None."""
    import re

    # 1. Explicit entry ID reference (e.g., "e-22" in commit text)
    id_matches = re.findall(r'e-\d+', commit_text)
    if id_matches:
        for entry in entries:
            if entry.get("id") in id_matches and entry.get("type") == "task":
                return entry

    # 2. Word overlap scoring against non-done tasks
    commit_tokens = _tokenize(commit_text)
    if not commit_tokens:
        return None

    candidates = []
    for entry in entries:
        if entry.get("type") != "task":
            continue
        if entry.get("status") == "done":
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
        # If only one non-done task exists, use it as default
        active_tasks = [e for e in entries if e.get("type") == "task" and e.get("status") != "done"]
        if len(active_tasks) == 1:
            return active_tasks[0]
        return None

    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    best_score, best_overlap, best_entry = candidates[0]
    # Require at least some meaningful overlap
    if best_overlap >= 1:
        return best_entry
    return None


def _check_duplicate_commit(entries, commit_hash):
    """Check if commit hash already exists in entries (including nested)."""
    for entry in entries:
        if entry.get("type") == "commit" and entry.get("meta", {}).get("hash", "").startswith(commit_hash):
            return True
        if _check_duplicate_commit(entry.get("entries", []), commit_hash):
            return True
    return False


def cmd_log():
    summary = os.environ.get("BEACON_SUMMARY", "")
    commit_hash = os.environ.get("BEACON_HASH", "")
    message = os.environ.get("BEACON_MESSAGE", "")
    date = os.environ.get("BEACON_DATE", "")
    ms_id = os.environ.get("BEACON_MS_ID", "")
    progress = os.environ.get("BEACON_PROGRESS", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    data = load_project()
    target = find_target_milestone(data, ms_id)

    entries = target.setdefault("entries", [])
    if _check_duplicate_commit(entries, commit_hash):
        if json_mode:
            print(json.dumps({"status": "duplicate", "hash": commit_hash}, ensure_ascii=False))
        else:
            print(f"Already logged: {commit_hash}")
        if progress:
            update_progress(target, progress)
            save_project(data)
        return

    commit_entry = {
        "id": next_entry_id(data),
        "type": "commit",
        "description": summary or message,
        "date": date,
        "created_at": date,
        "done_at": date,
        "status": "done",
        "meta": {"hash": commit_hash, "message": message},
    }

    # Try to auto-associate with a task
    commit_text = (summary or "") + " " + (message or "")
    matched_task = _find_matching_task(entries, commit_text)

    if matched_task:
        matched_task.setdefault("entries", []).append(commit_entry)
        if json_mode:
            print(json.dumps({"status": "logged", "hash": commit_hash,
                              "entry_id": commit_entry["id"],
                              "matched_task": matched_task["id"]}, ensure_ascii=False))
        else:
            print(f"Logged {commit_hash} → [{matched_task['id']}] {matched_task.get('description', '')}")
    else:
        entries.append(commit_entry)
        if json_mode:
            print(json.dumps({"status": "logged", "hash": commit_hash,
                              "entry_id": commit_entry["id"],
                              "milestone": target["id"]}, ensure_ascii=False))
        else:
            print(f"Logged {commit_hash} to {target['title']}")

    update_progress(target, progress)
    auto_update_summary(data)
    save_project(data)


def cmd_log_prepare():
    """Output context for AI-driven progress evaluation. No writes."""
    commit_hash = os.environ.get("BEACON_HASH", "")
    message = os.environ.get("BEACON_MESSAGE", "")
    date = os.environ.get("BEACON_DATE", "")
    ms_id = os.environ.get("BEACON_MS_ID", "")
    summary_text = os.environ.get("BEACON_SUMMARY", "")

    data = load_project()
    target = find_target_milestone(data, ms_id)
    entries = target.get("entries", [])
    total_tasks, done_tasks = _count_task_status_flat(entries)

    # Collect recent entries (last 5)
    all_flat = _collect_entries_flat(entries)
    all_flat.sort(key=lambda x: (x[0], x[1]), reverse=True)
    recent = [{"date": d, "description": desc} for d, _, desc in all_flat[:5]]

    # Pending tasks
    pending_tasks = []
    for e in entries:
        if e.get("type") == "task" and e.get("status") not in ("done", "cancelled"):
            pending_tasks.append({"id": e["id"], "description": e.get("description", "")})

    output = {
        "commit": {"hash": commit_hash, "message": message, "date": date, "summary": summary_text},
        "milestone": {
            "id": target["id"],
            "title": target.get("title", ""),
            "status": target.get("status", ""),
            "progress": target.get("progress", 0),
            "total_tasks": total_tasks,
            "done_tasks": done_tasks,
        },
        "pending_tasks": pending_tasks,
        "recent_entries": recent,
        "current_summary": data.get("summary", ""),
    }
    print(json.dumps(output, ensure_ascii=False))


def cmd_log_finalize():
    """Write progress and summary from AI evaluation. Called after prepare."""
    commit_hash = os.environ.get("BEACON_HASH", "")
    message = os.environ.get("BEACON_MESSAGE", "")
    date = os.environ.get("BEACON_DATE", "")
    ms_id = os.environ.get("BEACON_MS_ID", "")
    summary_text = os.environ.get("BEACON_SUMMARY", "")
    progress = os.environ.get("BEACON_PROGRESS", "")
    new_summary = os.environ.get("BEACON_NEW_SUMMARY", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    data = load_project()
    target = find_target_milestone(data, ms_id)

    # Record commit
    entries = target.setdefault("entries", [])
    if _check_duplicate_commit(entries, commit_hash):
        # Still update progress and summary even for duplicate
        if progress:
            update_progress(target, progress)
        if new_summary:
            data["summary"] = new_summary
        save_project(data)
        if json_mode:
            print(json.dumps({"status": "duplicate", "hash": commit_hash,
                              "progress": target.get("progress", 0)}, ensure_ascii=False))
        else:
            print(f"Already logged: {commit_hash} (updated progress/summary)")
        return

    commit_entry = {
        "id": next_entry_id(data),
        "type": "commit",
        "description": summary_text or message,
        "date": date,
        "created_at": date,
        "done_at": date,
        "status": "done",
        "meta": {"hash": commit_hash, "message": message},
    }

    commit_text = (summary_text or "") + " " + (message or "")
    matched_task = _find_matching_task(entries, commit_text)

    if matched_task:
        matched_task.setdefault("entries", []).append(commit_entry)
    else:
        entries.append(commit_entry)

    if progress:
        update_progress(target, progress)
    if new_summary:
        data["summary"] = new_summary

    save_project(data)

    result = {
        "status": "logged",
        "hash": commit_hash,
        "entry_id": commit_entry["id"],
        "progress": target.get("progress", 0),
        "summary_updated": bool(new_summary),
    }
    if matched_task:
        result["matched_task"] = matched_task["id"]
    else:
        result["milestone"] = target["id"]

    if json_mode:
        print(json.dumps(result, ensure_ascii=False))
    else:
        loc = f"[{matched_task['id']}]" if matched_task else target["title"]
        print(f"Logged {commit_hash} → {loc}")
        if progress:
            print(f"  Progress: {target.get('progress', 0)}%")
        if new_summary:
            print(f"  Summary updated.")


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


def _filter_cancelled(entries, show_all):
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
            filtered["entries"] = _filter_cancelled(children, show_all)
        result.append(filtered)
    return result


def cmd_task_list():
    ms_id = os.environ.get("BEACON_MS_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    show_all = os.environ.get("BEACON_ALL", "") == "1"
    data = load_project()
    target = find_target_milestone(data, ms_id)

    entries = _filter_cancelled(target.get("entries", []), show_all)

    if json_mode:
        output = {
            "milestone_id": target["id"],
            "milestone_title": target.get("title", ""),
            "entries": _entries_to_json(entries),
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
    result = find_entry(data, entry_id)
    if not result:
        print(f"Entry not found: {entry_id}")
        sys.exit(1)
    ms, _, entry, _ = result

    if json_mode:
        output = {
            "milestone_id": ms["id"],
            "milestone_title": ms.get("title", ""),
        }
        # Reuse _entries_to_json for single entry
        entry_json = _entries_to_json([entry])[0]
        output.update(entry_json)
        print(json.dumps(output, ensure_ascii=False))
        return

    icons = {"done": "\u25cf", "todo": "\u25cb", "in_progress": "\u25d0",
             "waiting": "\u25cc", "in_review": "\u25d1", "cancelled": "\u2718"}
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


def cmd_task_update():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()
    result = find_entry(data, entry_id)
    if not result:
        print(f"Entry not found: {entry_id}")
        sys.exit(1)
    ms, _, entry, _ = result

    description = os.environ.get("BEACON_DESCRIPTION", "")
    status = os.environ.get("BEACON_STATUS", "")
    detail = os.environ.get("BEACON_DETAIL", "")

    if description:
        entry["description"] = description
    if status:
        if status not in VALID_STATUSES:
            print(f"Invalid status: {status}. Valid: {', '.join(sorted(VALID_STATUSES))}")
            sys.exit(1)
        entry["status"] = status
        if status == "done" and not entry.get("done_at"):
            import datetime
            entry["done_at"] = datetime.date.today().isoformat()
    if detail:
        entry["detail"] = detail

    save_project(data)
    if json_mode:
        print(json.dumps(_entries_to_json([entry])[0], ensure_ascii=False))
    else:
        print(f"Updated: [{entry_id}] {entry.get('description', '')}")


def cmd_task_delete():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()
    result = find_entry(data, entry_id)
    if not result:
        print(f"Entry not found: {entry_id}")
        sys.exit(1)
    _, _, entry, _ = result

    entry["status"] = "cancelled"
    save_project(data)
    if json_mode:
        print(json.dumps({"id": entry_id, "status": "cancelled"}, ensure_ascii=False))
    else:
        print(f"Cancelled: [{entry_id}] {entry.get('description', '')}")


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


def cmd_retro_prepare():
    """Collect weekly data for retrospective. Read-only."""
    since = os.environ.get("BEACON_SINCE", "")
    until = os.environ.get("BEACON_UNTIL", "")

    data = load_project()

    # Collect entries from all milestones within the date range
    weekly_milestones = []
    for ms in data.get("milestones", []):
        ms_entries = []
        for entry in _collect_retro_entries_recursive(ms.get("entries", []), since, until):
            ms_entries.append(entry)
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


def _collect_retro_entries_recursive(entries, since, until):
    """Recursively collect entries within date range."""
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
        child_results = _collect_retro_entries_recursive(children, since, until) if children else []

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
    }
    fn = commands.get(cmd)
    if fn:
        fn()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
