#!/usr/bin/env python3
"""cmd_incident.py — the `beacon incident *` command family (ms-127 e-4320).

Extracted verbatim from commands.py (god-module split). Depends only on
commands_shared (upward) + leaf domain modules, never on commands.py — acyclic
(SPEC 方針4). commands.py re-imports these names for dispatch + `commands.X`.

C10 thin-action audit (ms-142 e-5527, spine §5): this family is the *pure
record* case — every verb is ``core.incident_*`` (record L2) + local
validation/filter/output (business L3), with **adapter = ∅** (no subprocess,
no outward effect). Already in the target shape, so no port extraction is
needed; recorded here as audited-and-conformant, not skipped.
"""

import json
import os
import sys

import core
from commands_shared import load_project, save_project


def cmd_incident_open():
    op_id = os.environ.get("BEACON_OPERATION_ID", "")
    title = os.environ.get("BEACON_INCIDENT_TITLE", "")
    description = os.environ.get("BEACON_INCIDENT_DESC", "")
    priority = os.environ.get("BEACON_INCIDENT_PRIORITY", "")
    if not op_id or not title:
        print("Error: -o <op-id> and incident title required")
        sys.exit(1)
    data = load_project()
    op, entry = core.incident_open(data, op_id, title=title, description=description, priority=priority)
    save_project(data, op={"type": "incident_open", "op_id": op_id, "entry_id": entry["id"], "title": title})
    if os.environ.get("BEACON_JSON"):
        print(json.dumps(entry, ensure_ascii=False))
    else:
        print(f"Incident opened: {entry['id']} \"{title}\" in {op_id}")


def cmd_incident_close():
    incident_id = os.environ.get("BEACON_INCIDENT_ID", "")
    resolution = os.environ.get("BEACON_INCIDENT_RESOLUTION", "")
    if not incident_id:
        print("Error: incident entry id required")
        sys.exit(1)
    data = load_project()
    container, entry = core.incident_close(data, incident_id, resolution=resolution)
    save_project(data, op={"type": "incident_close", "entry_id": incident_id, "resolution": resolution})
    if os.environ.get("BEACON_JSON"):
        print(json.dumps(entry, ensure_ascii=False))
    else:
        print(f"Incident resolved: {incident_id} \"{entry.get('title', '')}\"")
        if resolution:
            print(f"  Resolution: {resolution}")


def cmd_incident_escalate():
    incident_id = os.environ.get("BEACON_INCIDENT_ID", "")
    target_id = os.environ.get("BEACON_MS_ID", "")
    if not incident_id or not target_id:
        print("Error: incident id and -m <target-id> required")
        sys.exit(1)
    data = load_project()
    # ms-164 e-5947: resolve the escalation target GENERICALLY (any target class,
    # not just a milestone) here in the CLI layer — occupation.resolve_target is the
    # profession-agnostic resolver, and core (which does the append) cannot import
    # occupation. ``-m`` still names the target but now accepts any target id
    # (ms-… / opp-… / a descriptor class). A bad / unknown id raises a clear error.
    import occupation
    try:
        target = occupation.resolve_target(data, target_id)
    except ValueError as exc:
        print(f"Error: {exc}")
        sys.exit(1)
    tid = target.get("id", target_id)
    op, incident, task = core.incident_escalate(data, incident_id, target)
    # Keep ``ms_id`` in the audit op for back-compat; add generic ``target_id``.
    save_project(data, op={"type": "incident_escalate", "entry_id": incident_id,
                           "target_id": tid, "ms_id": tid, "task_id": task["id"]})
    print(f"Incident escalated: {incident_id} → {tid} as task {task['id']}")
    print(f"  {task['description']}")


def cmd_incident_list():
    """List incidents.

    Filters:
      -o <op-id>          only this Operation
      --status <status>   open / closed (default: all)
      --include-closed    shorthand for --status closed but additive
      --json              machine-readable output

    Used by:
      - /beacon-operation-review Step 6.5 (open Incident close 誘導)
      - /beacon-retrospect (past Incident history retrospection, UC10-O6 / e-619)
      - Direct CLI inspection
    """
    op_filter = os.environ.get("BEACON_OPERATION_ID", "")
    status_filter = os.environ.get("BEACON_INCIDENT_STATUS", "")
    include_closed = os.environ.get("BEACON_INCLUDE_CLOSED", "") == "1"
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    data = load_project()
    results = []
    for op in data.get("operations", []):
        if op_filter and op.get("id") != op_filter:
            continue
        for entry in op.get("entries", []):
            if entry.get("type") != "incident":
                continue
            entry_status = entry.get("status", "open")
            # status_filter wins; if not specified, default is to show open only
            # unless --include-closed is set. Treat "closed" / "resolved" /
            # "cancelled" all as "not open" for the include_closed shorthand
            # since Beacon's incident states are not fully standardized yet.
            if status_filter:
                if status_filter != entry_status:
                    continue
            elif not include_closed:
                if entry_status != "open":
                    continue
            results.append({
                "id": entry.get("id"),
                "op_id": op.get("id"),
                "op_title": op.get("title", ""),
                "title": entry.get("title", ""),
                "description": entry.get("description", ""),
                "status": entry_status,
                "priority": entry.get("priority", ""),
                "created_at": entry.get("created_at", ""),
                "closed_at": entry.get("closed_at", ""),
                "resolution": entry.get("resolution", ""),
            })

    # Most recent first
    results.sort(key=lambda r: r.get("created_at", ""), reverse=True)

    if json_mode:
        print(json.dumps(results, ensure_ascii=False))
        return

    if not results:
        if op_filter:
            print(f"No incidents found for {op_filter}.")
        else:
            print("No incidents found.")
        return

    for r in results:
        status_icon = "✓" if r["status"] == "closed" else "⚠"
        date_part = (r.get("closed_at") or r.get("created_at") or "")[:10]
        print(f"{status_icon} [{r['id']}] {r['title']}")
        print(f"  op: {r['op_id']} \"{r['op_title']}\" / {date_part} / status: {r['status']}")
        if r["status"] == "closed" and r.get("resolution"):
            print(f"  resolution: {r['resolution'][:120]}")
        elif r.get("description"):
            print(f"  desc: {r['description'][:120]}")
