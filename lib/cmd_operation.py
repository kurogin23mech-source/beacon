#!/usr/bin/env python3
"""cmd_operation.py — the `beacon operation *` command family (ms-127 e-4798).

Extracted verbatim from commands.py (god-module split). Depends only on
commands_shared (upward) + leaf domain modules (core / store), never on
commands.py — acyclic (SPEC 方針4). commands.py re-imports the PUBLIC handlers
for dispatch + `commands.X`; the family-private helper
`_fetch_active_operation_envelope` is NOT re-exported (patch it at
cmd_operation._fetch_active_operation_envelope — see the e-4320 re-export rule).

The review/spec/gate leaf helpers this family calls
(_fire_review_due_trigger / _gate_target_class / _spec_exists_for_op /
_ai_session_direct_completion_ban_active) were promoted to commands_shared in
this same change (e-4798-foundation) so operation retirement can fire the
目的達成 review nudge without importing commands.py.
"""

import json
import os
import sys
from typing import Optional

import core
from store import get_store
from commands_shared import (
    load_project,
    save_project,
    _actor_str,
    _local_date,
    _append_changelog,
    _resolve_current_author,
    _print_residual_dups,
    _is_cloud_mode,
    _get_api_client,
    _ai_session_direct_completion_ban_active,
    _fire_review_due_trigger,
    _gate_target_class,
    _spec_exists_for_op,
    _claim_occupation_for_work,
    _release_occupation_for_transition,
)


def cmd_operation_purge():
    """Hard-delete an operation record (op-N) — duplicate-ID recovery (e-863).

    The operation-level analogue of cmd_milestone_purge.

    Cloud mode (e-1030): routes through the server purge endpoint, which is
    owner-only.
    """
    op_id = os.environ.get("BEACON_OP_ID", "")
    reason = os.environ.get("BEACON_REASON", "")
    index_str = os.environ.get("BEACON_INDEX", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not op_id:
        print("Error: op-id is required.", file=sys.stderr)
        print("  Usage: beacon operation purge <op-id> --reason \"...\" [--index <n>]",
              file=sys.stderr)
        sys.exit(1)
    if not reason:
        print("Error: --reason is required for operation purge "
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

    # ms-84 Phase 2 (e-2036): Store.purge_operation unifies cloud + local.
    store = get_store()
    try:
        data = store.load_project()
    except (RuntimeError, ConnectionError) as e:
        print(f"Error loading project: {e}", file=sys.stderr)
        sys.exit(1)
    matches = core.find_operations(data, op_id)
    if not matches:
        print(f"Operation not found: {op_id}", file=sys.stderr)
        sys.exit(1)
    if len(matches) > 1 and index is None:
        print(f"Operation '{op_id}' has {len(matches)} duplicate records. "
              "Re-run with --index <n>:", file=sys.stderr)
        for i, o in enumerate(matches, 1):
            title = o.get("title", "(no title)")
            status = o.get("status", "?")
            print(f"  --index {i}  status={status}  title={title}", file=sys.stderr)
        sys.exit(1)

    try:
        result = store.purge_operation(op_id, reason=reason, index=index)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    purged = result["purged"]
    still_dirty = result["still_dirty"]
    dup_report = result["dup_report"]

    if not store.is_cloud():
        _append_changelog({
            "op": "operation_purge",
            "op_id": op_id,
            "index": index,
            "reason": reason,
            "purged_title": purged.get("title", ""),
        })

    if json_mode:
        print(json.dumps({
            "id": purged.get("id", op_id),
            "title": purged.get("title", ""),
            "purged": True,
            "still_dirty": still_dirty,
        }, ensure_ascii=False))
    else:
        print(f"Purged operation: [{purged.get('id', op_id)}] {purged.get('title', '')}")
        print(f"  Reason: {reason}")
        if still_dirty:
            _print_residual_dups(dup_report)


def cmd_operation_server_tick():
    # ms-107 e-3461 — Operation を server tick (trek tick 相乗り) の発火対象に
    # opt-in する。meta.server_tick を on/off し、任意で cadence_minutes を設定。
    # 内部専用: 有効化 setup 時に 1 回叩く (bin/beacon には出さない)。
    op_id = os.environ.get("BEACON_OP_ID", "")
    mode = (os.environ.get("BEACON_SERVER_TICK", "") or "on").strip().lower()
    cadence = os.environ.get("BEACON_CADENCE", "")
    data = load_project()
    matches = core.find_operations(data, op_id)
    if not matches:
        print(f"Error: Operation not found: {op_id}", file=sys.stderr)
        sys.exit(1)
    op = matches[0]
    meta = op.setdefault("meta", {})
    meta["server_tick"] = mode in ("on", "1", "true", "yes")
    if cadence:
        try:
            meta["cadence_minutes"] = int(cadence)
        except ValueError:
            print(f"Error: --cadence must be an integer, got {cadence!r}",
                  file=sys.stderr)
            sys.exit(1)
    save_project(data, op={"type": "operation_update", "op_id": op_id})
    state = "on" if meta["server_tick"] else "off"
    cad = meta.get("cadence_minutes", "default 60")
    print(f"Operation {op_id}: server_tick={state}, cadence_minutes={cad}")


def cmd_operation_open():
    title = os.environ.get("BEACON_OPERATION_TITLE", "")
    schedule = os.environ.get("BEACON_OPERATION_SCHEDULE", "weekdays")
    log_source = os.environ.get("BEACON_OPERATION_LOG_SOURCE", "")
    status = os.environ.get("BEACON_OPERATION_STATUS", "open")
    activation_hint = os.environ.get("BEACON_ACTIVATION_HINT", "")
    objective = os.environ.get("BEACON_OBJECTIVE", "")
    acceptance_criteria = os.environ.get("BEACON_ACCEPTANCE_CRITERIA", "")
    priority = os.environ.get("BEACON_PRIORITY", "")
    if not title:
        print("Error: operation title required")
        sys.exit(1)
    data = load_project()
    _gate_target_class(data, "operation")  # ms-115: block in non-dev projects
    # ms-43 / e-2281 — stamp the human author on the Operation so the Web
    # UI surfaces the creator label (= 起票者) instead of the legacy
    # ``"claude"`` literal in ``created_by``. Same resolution path as
    # cmd_milestone_add / cmd_task_add.
    author = _resolve_current_author(data)
    try:
        data, op = core.operation_open(
            data, title, schedule=schedule, log_source=log_source,
            status=status, activation_hint=activation_hint,
            objective=objective, acceptance_criteria=acceptance_criteria,
            priority=priority,
            author=author or None,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    # ms-142 T7 (e-5162): opening an Operation = this session starts working it →
    # stamp the live occupation claim so a second session touching the same op is
    # warned (the same 'someone is sitting here' layer a milestone gets on start).
    _claim_occupation_for_work(data, op["id"])
    save_project(data, op={"type": "operation_open", "op_id": op["id"], "title": title})
    if os.environ.get("BEACON_JSON"):
        print(json.dumps(op, ensure_ascii=False))
    else:
        print(f"Operation {op['status']}: {op['id']} \"{op['title']}\" [{op['schedule']['frequency']}]")


def cmd_operation_set_status():
    op_id = os.environ.get("BEACON_OPERATION_ID", "")
    status = os.environ.get("BEACON_OPERATION_STATUS", "")
    if not op_id or not status:
        print("Error: operation id and status required")
        sys.exit(1)
    data = load_project()
    try:
        op = core.operation_set_status(data, op_id, status)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data, op={"type": "operation_status", "op_id": op_id, "status": status})
    print(f"Operation {status}: {op_id} \"{op.get('title','')}\"")


def cmd_operation_update():
    op_id = os.environ.get("BEACON_OPERATION_ID", "")
    if not op_id:
        print("Error: operation id required")
        sys.exit(1)
    data = load_project()
    try:
        op = core.operation_update(
            data, op_id,
            title=os.environ.get("BEACON_TITLE", ""),
            schedule=os.environ.get("BEACON_OPERATION_SCHEDULE", ""),
            activation_hint=os.environ.get("BEACON_ACTIVATION_HINT", ""),
            objective=os.environ.get("BEACON_OBJECTIVE", ""),
            acceptance_criteria=os.environ.get("BEACON_ACCEPTANCE_CRITERIA", ""),
            priority=os.environ.get("BEACON_PRIORITY", ""),
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data, op={"type": "operation_update", "op_id": op_id})
    print(f"Operation updated: {op_id} \"{op.get('title','')}\"")


def cmd_operation_task_add():
    op_id = os.environ.get("BEACON_OPERATION_ID", "")
    description = os.environ.get("BEACON_DESCRIPTION", "")
    if not op_id or not description:
        print("Error: --op and description required")
        sys.exit(1)
    data = load_project()
    try:
        op, entry = core.operation_task_add(
            data, op_id, description,
            priority=os.environ.get("BEACON_PRIORITY", ""),
            motivation=os.environ.get("BEACON_MOTIVATION", ""),
            acceptance_criteria=os.environ.get("BEACON_ACCEPTANCE_CRITERIA", ""),
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data, op={"type": "operation_task_add", "op_id": op_id, "entry_id": entry["id"]})
    print(f"Added operation_task [{entry['id']}] to {op_id}: {description}")


def cmd_operation_task_done():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    reason = os.environ.get("BEACON_REASON", "")
    if not entry_id:
        print("Error: entry id required")
        sys.exit(1)
    if not reason:
        print("Error: --reason is required. Record why this task is done.", file=sys.stderr)
        sys.exit(1)
    data = load_project()
    try:
        entry = core.operation_task_done(data, entry_id, reason=reason)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data, op={"type": "operation_task_done", "entry_id": entry_id, "reason": reason})
    print(f"Done: [{entry_id}] {entry.get('description','')}\n  Reason: {reason}")


def cmd_operation_task_list():
    op_id = os.environ.get("BEACON_OPERATION_ID", "")
    if not op_id:
        print("Error: --op required")
        sys.exit(1)
    data = load_project()
    for op in data.get("operations", []):
        if op.get("id") == op_id:
            tasks = [e for e in op.get("entries", []) if e.get("type") == "operation_task"]
            if os.environ.get("BEACON_JSON"):
                print(json.dumps(tasks, ensure_ascii=False))
                return
            if not tasks:
                print(f"No operation_tasks in {op_id}")
                return
            for t in tasks:
                icon = "●" if t.get("status") == "done" else "○"
                pri = f" [{t['meta']['priority']}]" if t.get("meta", {}).get("priority") else ""
                print(f"  {icon} [{t['id']}]{pri} {t.get('description','')}")
            return
    print(f"Operation not found: {op_id}")
    sys.exit(1)


def cmd_operation_close():
    op_id = os.environ.get("BEACON_OPERATION_ID", "")
    if not op_id:
        print("Error: operation id required")
        sys.exit(1)
    # ms-119 / e-4008 — operation retirement is a completion claim; the same
    # non-bypassable gate applies. An AI session cannot close an operation
    # directly (route through the 目的達成 gate or declare a human signal).
    if _ai_session_direct_completion_ban_active():
        print(
            f"Error: closing {op_id} directly (without the review gate) from an "
            "AI session is refused (ms-119 / e-4008 structural guard).\n"
            "  Paths forward (= one of these):\n"
            f"    1. beacon target review-request {op_id} --new-state closed "
            "--intent ... — route through the 目的達成 gate (human approves).\n"
            "    2. BEACON_TARGET_COMPLETE_USER_OVERRIDE=1 — explicit user opt-in.\n"
            "    3. BEACON_SESSION_KIND=human — declare the session human-driven.",
            file=sys.stderr,
        )
        sys.exit(2)
    data = load_project()
    old_state = ""
    for _o in data.get("operations", []):
        if _o.get("id") == op_id:
            old_state = _o.get("status", "")
            break
    op = core.operation_close(data, op_id)
    # ms-142 T7 (e-5162): retiring an Operation frees its live occupation claim,
    # symmetric with milestone done/observe releasing theirs.
    _release_occupation_for_transition(data, op_id, reason="close")
    # ms-163 e-5879/5880: operation の retire も完遂 — generic な完遂 seam を発火して
    # deliverable 記録 + 完遂 decision を残す (milestone done と対称)。deliverable は
    # operation が slot を宣言しないので no-op、decision は完遂 verdict を記録する。save の
    # 前に呼ぶ (capture が data を in-memory で書き換え、caller がまとめて永続化する)。
    # on_target_completion は DIRECT 呼び出し必須 (helper 抽出は checker 被覆 credit を落とす)。
    import target_completion
    target_completion.on_target_completion(data, op, verdict="closed")
    save_project(data, op={"type": "operation_close", "op_id": op_id})
    # ms-119 e-3911: operation retirement is a completion claim — fire the
    # review-due nudge (目的達成 + 思想 if the operation has a SPEC).
    _fire_review_due_trigger(op_id, "operation", old_state, "closed",
                             target_title=op.get("title", ""),
                             has_spec=_spec_exists_for_op(op_id), gated=False)
    print(f"Operation closed: {op_id} \"{op.get('title', '')}\"")


def cmd_operation_pause():
    """PAUSE an Operation's execution cycle (ms-160 e-5814): move it to ``paused``
    so its scheduled fire is suppressed until resumed. The core mechanism
    (core.operation_pause) and the fire-suppression consumer (e-5484) already
    exist; this is the operator-facing verb that was missing."""
    op_id = os.environ.get("BEACON_OPERATION_ID", "")
    if not op_id:
        print("Error: operation id required (usage: beacon operation pause <op-id> [--reason ...])",
              file=sys.stderr)
        sys.exit(1)
    data = load_project()
    try:
        op = core.operation_pause(
            data, op_id,
            actor=_actor_str(),
            reason=os.environ.get("BEACON_REASON", ""),
        )
    except (ValueError, KeyError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data, op={"type": "operation_pause", "op_id": op_id})
    print(f"Operation paused: {op_id} \"{op.get('title', '')}\" "
          "(定期発火を抑止。resume で再開)")


def cmd_operation_resume():
    """RESUME a paused Operation (ms-160 e-5814): move it back to idle so the next
    scheduled fire is honoured again. Raises if the Operation is mid-cycle
    (due / running) rather than paused — the error names the recovery path."""
    op_id = os.environ.get("BEACON_OPERATION_ID", "")
    if not op_id:
        print("Error: operation id required (usage: beacon operation resume <op-id> [--reason ...])",
              file=sys.stderr)
        sys.exit(1)
    data = load_project()
    try:
        op = core.operation_resume(
            data, op_id,
            actor=_actor_str(),
            reason=os.environ.get("BEACON_REASON", ""),
        )
    except (ValueError, KeyError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data, op={"type": "operation_resume", "op_id": op_id})
    print(f"Operation resumed: {op_id} \"{op.get('title', '')}\" (定期発火を再開)")


def cmd_operation_list():
    data = load_project()
    ops = data.get("operations", [])
    if os.environ.get("BEACON_JSON"):
        print(json.dumps(ops, ensure_ascii=False))
        return
    if not ops:
        print("No operations.")
        return
    for op in ops:
        status_icon = "◐" if op.get("status") == "open" else "●"
        entries = op.get("entries", [])
        runs = [e for e in entries if e.get("type") == "run_record"]
        incidents = [e for e in entries if e.get("type") == "incident" and e.get("status") == "open"]
        last_run = f" last: {_local_date(runs[-1]['date'])} {runs[-1]['status']}" if runs else ""
        incident_info = f" ⚠ {len(incidents)} incident(s)" if incidents else ""
        print(f"{status_icon} {op['id']} \"{op.get('title', '')}\" [{op.get('schedule', {}).get('frequency', '')}]{last_run}{incident_info}")


def _fetch_active_operation_envelope(op_id: str):
    """Cloud-mode helper: fetch the active T2 envelope for ``op_id`` if any.

    Returns ``None`` in local mode, on auth issues, or if the server is not
    reachable. Used by ``cmd_operation_show`` for the envelope section and
    by ``cmd_operation_revoke`` to default the envelope id to the active one.
    """
    if not _is_cloud_mode():
        return None
    try:
        client, config = _get_api_client()
        records = client.list_operation_envelopes(
            config["project_id"], op_id, status="active"
        )
    except Exception:
        return None
    return records[0] if records else None


def cmd_operation_show():
    op_id = os.environ.get("BEACON_OPERATION_ID", "")
    if not op_id:
        print("Error: operation id required")
        sys.exit(1)
    data = load_project()
    json_mode = bool(os.environ.get("BEACON_JSON"))
    for op in data.get("operations", []):
        if op.get("id") == op_id:
            # Augment with envelope record (cloud mode only).
            active_env = _fetch_active_operation_envelope(op_id)
            if json_mode:
                payload = dict(op)
                if active_env is not None:
                    payload["active_envelope"] = active_env
                print(json.dumps(payload, ensure_ascii=False))
            else:
                print(f"{op['id']} \"{op.get('title', '')}\" [{op.get('status', '')}]")
                if active_env:
                    env = active_env.get("envelope", {})
                    created = active_env.get("created_at", "")[:10]
                    expires = env.get("expires_at", "")[:10]
                    print(f"  Active envelope: {active_env.get('envelope_id', '')[:12]}…  "
                          f"(issued {created} by {active_env.get('created_by', '')})")
                    actions = active_env.get("approved_actions", [])
                    if actions:
                        print(f"    Approved actions:")
                        for a in actions:
                            print(f"      - {a}")
                    if expires:
                        print(f"    Expires: {expires}  (revoke to invalidate)")
                else:
                    print(f"  Envelope: none active  "
                          f"(autonomous execution disabled — run "
                          f"`beacon operation approve {op_id} --spec <doc-id>`)")
                for e in op.get("entries", []):
                    if e.get("type") == "run_record":
                        icon = {"ok": "✓", "warning": "⚠", "error": "✗"}.get(e.get("status", ""), "?")
                        print(f"  {icon} {e['date'][:10]} {e.get('batch', '')} — {e.get('description', '')}")
                    elif e.get("type") == "incident":
                        icon = "⚠" if e.get("status") == "open" else "✓"
                        print(f"  {icon} [{e['id']}] {e.get('title', '')} [{e.get('status', '')}]")
            return
    print(f"Operation not found: {op_id}")
    sys.exit(1)


def cmd_operation_approve():
    """Mint a T2 envelope from a SPEC doc's ``approved_actions`` (ms-60 / e-1339).

    Cloud-mode only. Local mode is rejected with a clear message — local
    project.json doesn't have anywhere to put a server-signed envelope
    record (the security model needs a server signing key).
    """
    op_id = os.environ.get("BEACON_OPERATION_ID", "")
    spec_doc_id = os.environ.get("BEACON_SPEC_DOC_ID", "")
    ttl_seconds_str = os.environ.get("BEACON_TTL_SECONDS", "")
    json_mode = bool(os.environ.get("BEACON_JSON"))

    if not op_id:
        print("Error: operation id required", file=sys.stderr)
        sys.exit(1)
    if not spec_doc_id:
        print("Error: --spec <doc-id> required (the SPEC doc whose "
              "approved_actions to authorize)", file=sys.stderr)
        sys.exit(1)

    if not _is_cloud_mode():
        print("Error: operation approve requires cloud mode "
              "(envelope signing needs a server key). Run "
              "'beacon cloud upload-initial' first.", file=sys.stderr)
        sys.exit(1)

    ttl_seconds = None
    if ttl_seconds_str:
        try:
            ttl_seconds = int(ttl_seconds_str)
            if ttl_seconds <= 0:
                raise ValueError
        except ValueError:
            print(f"Error: --ttl-seconds must be a positive integer "
                  f"(got {ttl_seconds_str!r})", file=sys.stderr)
            sys.exit(1)

    client, config = _get_api_client()
    try:
        record = client.operation_approve(
            config["project_id"], op_id,
            spec_doc_id=spec_doc_id, ttl_seconds=ttl_seconds,
        )
    except Exception as exc:
        print(f"Error: approve failed: {exc}", file=sys.stderr)
        sys.exit(1)

    if json_mode:
        print(json.dumps(record, ensure_ascii=False))
        return
    env_id = record.get("envelope_id", "")
    env = record.get("envelope", {})
    actions = record.get("approved_actions", [])
    print(f"Approved: {op_id}")
    print(f"  envelope: {env_id}")
    print(f"  spec doc: {record.get('spec_doc_id', '')}")
    print(f"  issuer:   {record.get('created_by', '')}")
    print(f"  expires:  {env.get('expires_at', '')[:10]}")
    print(f"  approved actions ({len(actions)}):")
    for a in actions:
        print(f"    - {a}")


def cmd_operation_envelope_verify():
    """Check whether a requested action is permitted by the active envelope.

    ms-60 / e-1340 — the AI self-check primitive. Called by the
    ``/beacon-operation-execute`` Skill before running each action.

    Output (json mode):
      {"op_id": ..., "action": ..., "envelope_id": ...|null,
       "active": bool, "permitted": bool,
       "approved_actions": [...], "reason": "..."}

    Exit codes:
      0 — permitted (active envelope + action matches approved_actions)
      1 — not permitted (no active envelope, or action outside scope)
      2 — usage / cloud error

    Designed to be easy to call from a Skill: ``beacon operation envelope
    verify op-X "extract:profile:user-1" --json`` returns a one-shot decision.
    """
    op_id = os.environ.get("BEACON_OPERATION_ID", "")
    action = os.environ.get("BEACON_ACTION", "")
    json_mode = bool(os.environ.get("BEACON_JSON"))

    if not op_id:
        print("Error: operation id required", file=sys.stderr)
        sys.exit(2)
    if not action:
        print("Error: action required (e.g. 'extract:profile:user-1')",
              file=sys.stderr)
        sys.exit(2)
    if not _is_cloud_mode():
        print("Error: envelope verify requires cloud mode.", file=sys.stderr)
        sys.exit(2)

    # Reuse server/approved_actions matcher as single source of truth.
    sys.path.insert(
        0, os.path.join(os.path.dirname(__file__), "..", "server")
    )
    try:
        import approved_actions as aa
    except ImportError as exc:
        print(f"Error: cannot load approved_actions module: {exc}",
              file=sys.stderr)
        sys.exit(2)

    try:
        client, config = _get_api_client()
        records = client.list_operation_envelopes(
            config["project_id"], op_id, status="active"
        )
    except Exception as exc:
        print(f"Error: cannot fetch envelope: {exc}", file=sys.stderr)
        sys.exit(2)

    active = records[0] if records else None
    if active is None:
        result = {
            "op_id": op_id, "action": action,
            "envelope_id": None, "active": False, "permitted": False,
            "approved_actions": [],
            "reason": "no active envelope — operation not approved",
        }
    else:
        approved = active.get("approved_actions", [])
        permitted = aa.matches(approved, action)
        result = {
            "op_id": op_id, "action": action,
            "envelope_id": active.get("envelope_id"),
            "active": True, "permitted": permitted,
            "approved_actions": approved,
            "reason": (
                "action matches approved_actions"
                if permitted
                else "action outside approved_actions — escalate or refuse"
            ),
        }

    if json_mode:
        print(json.dumps(result, ensure_ascii=False))
    else:
        flag = "✓ permitted" if result["permitted"] else "✗ not permitted"
        print(f"{flag}: {op_id} action={action!r}")
        print(f"  reason: {result['reason']}")
        if result["envelope_id"]:
            print(f"  envelope: {result['envelope_id']}")
        if result["approved_actions"]:
            print(f"  approved actions ({len(result['approved_actions'])}):")
            for a in result["approved_actions"]:
                print(f"    - {a}")

    sys.exit(0 if result["permitted"] else 1)


def cmd_operation_revoke():
    """Mark the active envelope on ``op_id`` as revoked (ms-60 / e-1339).

    Without ``--envelope-id``, the current active envelope is revoked. If
    no active envelope exists, this is an error so the caller doesn't
    silently no-op.
    """
    op_id = os.environ.get("BEACON_OPERATION_ID", "")
    envelope_id = os.environ.get("BEACON_ENVELOPE_ID", "")
    reason = os.environ.get("BEACON_REASON", "") or "manual revoke"
    json_mode = bool(os.environ.get("BEACON_JSON"))

    if not op_id:
        print("Error: operation id required", file=sys.stderr)
        sys.exit(1)

    if not _is_cloud_mode():
        print("Error: operation revoke requires cloud mode.", file=sys.stderr)
        sys.exit(1)

    client, config = _get_api_client()
    if not envelope_id:
        active = _fetch_active_operation_envelope(op_id)
        if not active:
            print(f"Error: no active envelope for {op_id}. "
                  f"Pass --envelope-id <id> to revoke a specific one, "
                  f"or check `beacon operation show {op_id}`.",
                  file=sys.stderr)
            sys.exit(1)
        envelope_id = active.get("envelope_id", "")

    try:
        record = client.operation_revoke(
            config["project_id"], op_id, envelope_id, reason=reason,
        )
    except Exception as exc:
        print(f"Error: revoke failed: {exc}", file=sys.stderr)
        sys.exit(1)

    if json_mode:
        print(json.dumps(record, ensure_ascii=False))
        return
    print(f"Revoked: {op_id} envelope {envelope_id}")
    print(f"  reason: {record.get('revoke_reason', reason)}")
    print(f"  revoked_at: {record.get('revoked_at', '')[:19]}")
