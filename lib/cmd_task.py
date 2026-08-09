#!/usr/bin/env python3
"""cmd_task.py — the `beacon task *` and `beacon entry *` command families (ms-127 e-4319).

Extracted verbatim from commands.py (the god-module split). Holds the task /
entry CLI handlers. Depends only on commands_shared (upward) + leaf domain
modules (core / work_model / store), never on commands.py — acyclic (SPEC 方針4).
commands.py re-imports these names so `import commands; commands.cmd_task_add()`
and the dispatch dict keep resolving.
"""

import json
import os
import sys
from typing import Optional

from store import get_store
import core
import work_model

from commands_shared import (
    load_project,
    save_project,
    _append_changelog,
    _check_ms_status_for_write,
    _require_reason_or_skip,
    _resolve_current_author,
    _human_untriaged_bypass_refused,
    _HUMAN_UNTRIAGED_REFUSED_MSG,
    _print_residual_dups,
)


# --- entry purge ---

def cmd_entry_purge():
    """Hard-delete an entry record (e-N) — duplicate-ID recovery (e-863).

    The entry-level analogue of cmd_milestone_purge. Loads via
    load_project_unsafe so it works on a project that fails validation, and
    falls back to an unsafe save while residual duplicates remain.

    Cloud mode (e-1030): routes through the server purge endpoint, which is
    owner-only.
    """
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    reason = os.environ.get("BEACON_REASON", "")
    index_str = os.environ.get("BEACON_INDEX", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not entry_id:
        print("Error: entry-id is required.", file=sys.stderr)
        print("  Usage: beacon entry purge <e-id> --reason \"...\" [--index <n>]",
              file=sys.stderr)
        sys.exit(1)
    if not reason:
        print("Error: --reason is required for entry purge "
              "(audit trail per CORE doc data-immutability-principle).",
              file=sys.stderr)
        sys.exit(1)
    index: Optional[int] = None
    if index_str:
        try:
            index = int(index_str)
        except ValueError:
            print(f"Error: --index must be an integer, got '{index_str}'.",
                  file=sys.stderr)
            sys.exit(1)

    # ms-84 Phase 2 (e-2036): Store.purge_entry unifies cloud + local paths.
    store = get_store()
    try:
        data = store.load_project()
    except (RuntimeError, ConnectionError) as e:
        print(f"Error loading project: {e}", file=sys.stderr)
        sys.exit(1)
    matches = core.find_entries(data, entry_id)
    if not matches:
        print(f"Entry not found: {entry_id}", file=sys.stderr)
        sys.exit(1)
    if len(matches) > 1 and index is None:
        print(f"Entry '{entry_id}' has {len(matches)} duplicate records. "
              "Re-run with --index <n>:", file=sys.stderr)
        for i, e in enumerate(matches, 1):
            desc = e.get("description", "(no description)")
            etype = e.get("type", "?")
            print(f"  --index {i}  type={etype}  desc={desc[:60]}", file=sys.stderr)
        sys.exit(1)

    try:
        result = store.purge_entry(entry_id, reason=reason, index=index)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    purged = result["purged"]
    still_dirty = result["still_dirty"]
    dup_report = result["dup_report"]

    if not store.is_cloud():
        _append_changelog({
            "op": "entry_purge",
            "entry_id": entry_id,
            "index": index,
            "reason": reason,
            "purged_desc": purged.get("description", ""),
        })

    if json_mode:
        print(json.dumps({
            "id": purged.get("id", entry_id),
            "description": purged.get("description", ""),
            "purged": True,
            "still_dirty": still_dirty,
        }, ensure_ascii=False))
    else:
        print(f"Purged entry: [{purged.get('id', entry_id)}] "
              f"{purged.get('description', '')[:80]}")
        print(f"  Reason: {reason}")
        if still_dirty:
            _print_residual_dups(dup_report)


# --- task family ---

def cmd_task_add():
    description = os.environ.get("BEACON_DESCRIPTION", "")
    ms_id = os.environ.get("BEACON_MS_ID", "")
    entry_type = os.environ.get("BEACON_TYPE", "task")
    date = os.environ.get("BEACON_DATE", "")
    detail = os.environ.get("BEACON_DETAIL", "")
    requested_by = os.environ.get("BEACON_REQUESTED_BY", "")
    priority = os.environ.get("BEACON_PRIORITY", "")
    motivation = os.environ.get("BEACON_MOTIVATION", "")
    acceptance_criteria = os.environ.get("BEACON_ACCEPTANCE_CRITERIA", "")
    deadline = os.environ.get("BEACON_DEADLINE", "")  # ms-139 e-4949
    # ms-126: priority mandatory on the human path; machine callers opt into the
    # ``untriaged`` sentinel via ``--untriaged`` (BEACON_ALLOW_UNTRIAGED=1).
    allow_untriaged = os.environ.get("BEACON_ALLOW_UNTRIAGED", "") == "1"
    # ms-126 / e-4222: refuse the untriaged escape hatch on a human session — a
    # person must pick a real priority; only machine / AI paths may defer with
    # the untriaged sentinel. See _human_untriaged_bypass_refused. Checked before
    # the ms-81 re-open prompt so a human --untriaged is rejected up front,
    # regardless of the target milestone's state.
    if _human_untriaged_bypass_refused():
        print(f"Error: {_HUMAN_UNTRIAGED_REFUSED_MSG}", file=sys.stderr)
        sys.exit(1)

    data = load_project()
    target = core.find_target_milestone(data, ms_id)

    # ms-81 e-1919: re-open prompt for done MS. Adding a task to a done
    # milestone creates a zombie (= the e-1916 write gate then blocks any
    # commit/done against it, so it would stay in todo forever). Per SPEC
    # §5 the right move is to surface the choice: re-open into observing
    # (the natural recovery slot) or active, or abort. Interactive only;
    # in the non-interactive Skill / hook path we proceed with a warning
    # so the Skill report can flag the audit trail rather than block.
    if target.get("status") == "done":
        if sys.stdin.isatty():
            print(
                f"\n[ms-81 re-open prompt] [{target['id']}] {work_model.target_label(target)} "
                f"is done. Adding a task here will leave it stuck (the "
                f"write gate blocks commit / done on done milestones).",
                file=sys.stderr,
            )
            print(
                "   Options: (o) re-open as observing, (a) re-open as active, "
                "(s) skip prompt and add anyway, (n) abort",
                file=sys.stderr,
            )
            try:
                choice = input("   Choice [o/a/s/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                choice = "n"
            if choice == "o":
                core.milestone_update(
                    data, ms_id, status="observing",
                    reason="re-opened to add task",
                )
                print(f"  re-opened {ms_id} as observing", file=sys.stderr)
            elif choice == "a":
                core.milestone_start(data, ms_id)
                print(f"  re-opened {ms_id} as active", file=sys.stderr)
            elif choice in ("", "n"):
                print("  aborted (no task added)", file=sys.stderr)
                sys.exit(1)
            # "s" falls through and adds the task without changing status
        else:
            print(
                f"[ms-81 re-open warning] adding to done MS [{target['id']}] "
                f"{work_model.target_label(target)} — task will be stuck (write gate blocks "
                f"future commits / done). Re-open with `beacon milestone start "
                f"{ms_id}` or `beacon milestone observe {ms_id}` first.",
                file=sys.stderr,
            )

    # ms-43 / e-2281 — stamp the human author on the task so the Web UI
    # surfaces the creator label (= 起票者) instead of the legacy
    # ``"claude"`` literal in ``meta.created_by``. Same resolution path
    # as cmd_milestone_add / cmd_operation_open.
    author = _resolve_current_author(data)
    try:
        eid = core.task_add(data, ms_id, description, entry_type=entry_type,
                            date=date, detail=detail, requested_by=requested_by,
                            priority=priority, motivation=motivation,
                            acceptance_criteria=acceptance_criteria,
                            deadline=deadline, author=author or None,
                            allow_untriaged=allow_untriaged)
    except ValueError as e:
        # ms-126: clean CLI error for the "priority is required" forcing
        # function (and the existing "Invalid priority").
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    from_str = f" (from {requested_by})" if requested_by else ""
    print(f"Added {entry_type} [{eid}] to {work_model.target_label(target)}: {description}{from_str}")


def cmd_task_done():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    progress = os.environ.get("BEACON_PROGRESS", "")
    reason = _require_reason_or_skip("task done")
    import datetime
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    data = load_project()
    # ms-81 e-1916: status gate. Look up the entry's parent MS first so we
    # can warn if the MS isn't write-authorised. The PR-merge sub-branch
    # below also goes through the same gate.
    result = core.find_entry(data, entry_id)
    if result:
        parent_ms, _, entry, _ = result
        if not _check_ms_status_for_write(
            parent_ms, f"task done {entry_id}"
        ):
            sys.exit(1)
        if entry.get("type") == "pr":
            pr_status = entry.get("meta", {}).get("pr_status", "")
            if pr_status not in ("approved", "merged"):
                print(
                    f"Warning: PR [{entry_id}] has pr_status='{pr_status}'. "
                    "Use 'beacon pr merge' to merge explicitly, or 'beacon pr close' to close without merging.",
                    file=sys.stderr,
                )
            ms, entry = core.pr_merge(data, entry_id, date=today)
            print(f"Merged PR [{entry_id}]: {entry['description']}")
            core.update_progress(ms, progress)
            if progress:
                print(f"  Progress: {ms.get('progress', 0)}%")
            save_project(data)
            return
    ms, entry = core.task_done(data, entry_id, date=today, reason=reason)
    print(f"Done: [{entry_id}] {entry['description']}")
    if reason:
        print(f"  Reason: {reason}")
    core.update_progress(ms, progress)
    if progress:
        print(f"  Progress: {ms.get('progress', 0)}%")
    save_project(data, op={"op": "task_done", "entry_id": entry_id, "reason": reason})
    issue_number = entry.get("meta", {}).get("issue_number")
    if issue_number:
        print(f"  Linked Issue: #{issue_number} — close it with: gh issue close {issue_number}")


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
        print(f"No entries in {work_model.target_label(target)}")
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
        output = {"milestone_id": ms["id"], "milestone_title": work_model.target_label(ms)}
        entry_json = core.entries_to_json([entry])[0]
        output.update(entry_json)
        print(json.dumps(output, ensure_ascii=False))
        return

    icons = {"done": "\u25cf", "todo": "\u25cb", "in_progress": "\u25d1",
             "waiting": "\u25cc", "in_review": "\u25d5", "observing": "\u25d5",
             "cancelled": "\u2718"}
    icon = icons.get(entry.get("status", "todo"), "?")
    print(f"{icon} [{entry['id']}] {entry.get('description', '')}")
    print(f"  Milestone: [{ms['id']}] {work_model.target_label(ms)}")
    print(f"  Type: {entry.get('type', '?')}  Status: {entry.get('status', '?')}")
    priority = entry.get("meta", {}).get("priority", "")
    if priority:
        print(f"  Priority: {priority}")
    print(f"  Created: {entry.get('created_at', '-')}  Done: {entry.get('done_at', '-')}")
    if entry.get("motivation"):
        print(f"  Why: {entry['motivation']}")
    if entry.get("acceptance_criteria"):
        print(f"  Done when: {entry['acceptance_criteria']}")
    if entry.get("behavior"):
        print(f"  Behavior: {entry['behavior']}")
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
    ms_id = os.environ.get("BEACON_MS_ID", "")
    import datetime
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    data = load_project()
    try:
        if ms_id:
            core.entry_move(data, entry_id, ms_id=ms_id)
        ms, entry = core.task_update(
            data, entry_id,
            description=os.environ.get("BEACON_DESCRIPTION", ""),
            status=os.environ.get("BEACON_STATUS", ""),
            detail=os.environ.get("BEACON_DETAIL", ""),
            date=today,
            motivation=os.environ.get("BEACON_MOTIVATION", ""),
            acceptance_criteria=os.environ.get("BEACON_ACCEPTANCE_CRITERIA", ""),
            behavior=os.environ.get("BEACON_BEHAVIOR", ""),
            priority=os.environ.get("BEACON_PRIORITY", ""),
            deadline=os.environ.get("BEACON_DEADLINE", ""),  # ms-139 e-4949
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
    print("Error: 'beacon task delete' is deprecated. Use 'beacon task cancel' instead.")
    print("  Physical deletion is not allowed — all cancellations are soft and traceable.")
    print("  Example: beacon task cancel <entry-id> --reason \"重複タスクのため\"")
    sys.exit(1)


def cmd_task_cancel():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    reason = os.environ.get("BEACON_REASON", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()
    try:
        entry = core.task_delete(data, entry_id, reason=reason)
    except ValueError as e:
        print(str(e))
        sys.exit(1)
    save_project(data, op={"op": "task_cancel", "entry_id": entry_id, "reason": reason})
    if json_mode:
        print(json.dumps({"id": entry_id, "status": "cancelled"}, ensure_ascii=False))
    else:
        print(f"Cancelled: [{entry_id}] {entry.get('description', '')}")
        if reason:
            print(f"  Reason: {reason}")


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
