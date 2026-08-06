#!/usr/bin/env python3
"""cmd_trek.py — the `beacon trek *` command family (ms-127 e-4820).

Extracted verbatim from commands.py (god-module split). Depends only on
commands_shared (upward) + leaf domain modules (core / store / work_model),
never on commands.py — acyclic (SPEC 方針4). commands.py re-imports the PUBLIC
handlers for dispatch + `commands.X`; family-private helpers (_arm_for_trek /
_trek_transition / _scope_matches_* / _cmd_trek_blanket / _cloud_slot_client /
_collect_trek_local_aggregation / _goal_state_status / _resolve_local_session_id
/ _trek_join_consent_gate / _build_trek_join_consent_explanation) are NOT
re-exported (patch them at cmd_trek.<name>).

The docs / frontmatter / project-id leaf helpers this family shares with
commands.py callers (_current_project_id / _get_docs_dir / _read_local_doc, and
the _parse_frontmatter + DEFAULT_SCOPE they pull in) were promoted to
commands_shared in this same change (e-4820-foundation) so the many doc / account
/ operation callers in commands.py can keep using them without importing cmd_trek
(which would form a cycle).

Test patch target (monkeypatch trap): a test driving a cmd_trek_* handler patches
helpers the handler resolves in cmd_trek's own namespace (cmd_trek._X), including
re-exported ones — `commands._X` is an independent binding and a silent no-op on
the cmd_trek call path (the e-4320 rule).
"""

import json
import os
import sys
from typing import Optional

import core
import work_model
from store import get_store
from commands_shared import (
    load_project,
    save_project,
    _resolve_session_id,
    _resolve_creator_identity,
    _get_api_client,
    _is_cloud_mode,
    _bus_auto_execute_channels,
    _mirror_auto_execute_channels_to_local,
    _write_bus_budget,
    _current_project_id,
    _get_docs_dir,
    _read_local_doc,
)

# ms-127 e-4820: trek-only module constants (moved verbatim from commands.py with the family — used only by the trek handlers/helpers here).

TREK_AUTO_ARM_CHANNELS = (
    "trek-progress-check",
    "trek-trigger",
    "trek-task-review",
    # ms-92 / e-2164 — leader-digest channel. Leaders receive an
    # aggregated per-session status snapshot on the same cadence as
    # trek-progress-check. The channel is auto-execute so the leader's
    # AI session can render the digest immediately without waiting for
    # human Skill invocation.
    "trek-leader-digest",
)
TREK_AUTO_ARM_DEFAULT_BUDGET = 20


# ms-88 / e-2090 — Trek 参加 per-session 明示同意 gate。
# Trek 参加 = scope 内 DM blanket 自動承認 + autonomous loop 入場の合算で
# turn 制限なく AI を動かす権限委譲。 typed-ack を求めることで user が
# consequence を理解せず参加する構造的危険を塞ぐ。
TREK_JOIN_CONSENT_PHRASE = "I UNDERSTAND"


def cmd_trek_create():
    """Create a new trek (= top-level cross-project collaboration area).

    Reads from env:
      BEACON_TREK_TITLE       (required) trek title
      BEACON_TREK_TYPE        temporary | persistent (default persistent)
      BEACON_TREK_DESCRIPTION free-form description (optional)
      BEACON_USER_ID          creator user_id (fallback: whoami)
      BEACON_USER_EMAIL       creator email (required)
      BEACON_SESSION_ID       creator session_id (required, becomes leader)
      BEACON_JSON             "1" → emit json instead of human text
    """
    import trek
    import trek_store

    title = os.environ.get("BEACON_TREK_TITLE", "").strip()
    type_ = os.environ.get("BEACON_TREK_TYPE", "").strip() or "persistent"
    description = os.environ.get("BEACON_TREK_DESCRIPTION", "")
    # ms-75 / e-1865: optional acceptance criterion / completion marker for the
    # trek. Empty = "leader decides", non-empty = explicit signal that members
    # can match against to suggest archive.
    goal_state = os.environ.get("BEACON_TREK_GOAL_STATE", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not title:
        print("Error: trek title is required (--title or positional arg)",
              file=sys.stderr)
        sys.exit(1)

    user_id, email, session_id = _resolve_creator_identity()
    if not email:
        print(
            "Error: BEACON_USER_EMAIL is required to create a trek "
            "(= recorded as creator/leader member email).\n"
            "  How to set: it is normally inherited from a logged-in session — "
            "run `beacon auth login` (or from an active bclaude session), or "
            "export BEACON_USER_EMAIL=<you@example.com> for a scripted call.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not session_id:
        print(
            "Error: BEACON_SESSION_ID is required to create a trek "
            "(= the session that creates becomes initial leader; SPEC 方針 9).\n"
            "  How to set: it is minted per bclaude session — run this from an "
            "active bclaude session, or export BEACON_SESSION_ID=<session-id> "
            "(see `beacon session id`) for a scripted call.",
            file=sys.stderr,
        )
        sys.exit(1)

    if _is_cloud_mode():
        # Cloud path: server resolves creator identity from auth token.
        # BEACON_USER_ID / BEACON_USER_EMAIL are ignored server-side (kept
        # for local-mode parity).
        try:
            client, _config = _get_api_client()
            new_doc = client.create_trek(
                title=title,
                creator_session_id=session_id,
                description=description,
                type_=type_,
            )
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        try:
            new_doc = trek.new_trek(
                title=title,
                creator_user_id=user_id,
                creator_email=email,
                creator_session_id=session_id,
                description=description,
                type_=type_,
                goal_state=goal_state,
            )
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        trek_store.save_trek(new_doc)

    if json_mode:
        print(json.dumps(new_doc, ensure_ascii=False))
    else:
        print(f"Created trek {new_doc['trek_id']} \"{new_doc['title']}\" "
              f"({new_doc['type']}, status={new_doc['status']})")
        print(f"  leader session: {new_doc['leader_session_id']}")
        print(f"  creator: {new_doc['creator_actor']['email']}")
        print("  next: `beacon trek plan` で scope を追加、"
              "`beacon trek invite` でメンバーを呼ぶ、"
              "`beacon trek start <id>` で active に")


def cmd_trek_list():
    """List treks. By default hides archived; --all includes them.

    Reads from env:
      BEACON_TREK_STATUS       optional status filter
      BEACON_TREK_INCLUDE_ARCHIVED  "1" → include archived
      BEACON_USER_ID           visibility filter (defaults to current user
                               so the listing is per-actor; pass --all to
                               disable from CLI)
      BEACON_TREK_ALL_ACTORS   "1" → disable actor filter (admin view)
      BEACON_JSON              "1" → emit json
    """
    import trek_store

    status_filter = os.environ.get("BEACON_TREK_STATUS", "").strip() or None
    include_archived = os.environ.get("BEACON_TREK_INCLUDE_ARCHIVED", "") == "1"
    all_actors = os.environ.get("BEACON_TREK_ALL_ACTORS", "") == "1"
    # ms-75 / e-1813: filter to treks the current user has actually joined
    # (= members[].joined_at non-empty for them). Pending invitations stay
    # out of the joined list so /beacon-session-start can display "current
    # treks" without mixing in invitations the user hasn't yet accepted.
    joined_only = os.environ.get("BEACON_TREK_JOINED_ONLY", "") == "1"
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if all_actors:
        actor_id = None
        actor_email = ""
    else:
        user_id, actor_email, _ = _resolve_creator_identity()
        actor_id = user_id or None

    # ms-84 Phase 2: Store 経由で cloud / local を統一。 actor_id (= local-only
    # filter) と all_actors (= cloud admin view) はそれぞれ片方の backend が
    # ignore する設計。 cloud transport / 403 は RuntimeError として呼び出し
    # 側 (= ここ) で従来通り display する。
    try:
        treks = get_store().list_treks(
            actor_id=actor_id,
            status=status_filter or "",
            include_archived=include_archived,
            all_actors=all_actors,
        )
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if joined_only:
        # Walk members[] for an entry matching the caller (user_id or email)
        # whose ``joined_at`` is non-empty. This is the only structurally
        # reliable join check — bare visibility (= creator/member presence)
        # would include treks the user was invited to but never accepted.
        def _is_joined(t: dict) -> bool:
            for m in t.get("members") or []:
                if (actor_id and m.get("user_id") == actor_id) \
                        or (actor_email and m.get("email") == actor_email):
                    if m.get("joined_at"):
                        return True
            return False
        treks = [t for t in treks if _is_joined(t)]

    if json_mode:
        print(json.dumps(treks, ensure_ascii=False, indent=2))
        return

    if not treks:
        if joined_only:
            print("(no joined treks — `beacon trek join <id>` で招待を承諾)")
        elif actor_id:
            print(f"(no treks visible to {actor_id} — try --all で全件)")
        else:
            print("(no treks yet — `beacon trek create \"title\"` で最初の trek を起票)")
        return

    print(f"Treks ({len(treks)}):")
    # ms-97 / e-2659 (AC8 + AC31): collect treks with grandfathered
    # project-wide scope so the listing surfaces the warning once at the
    # bottom (= avoid spamming every row).
    project_wide_treks: list[tuple[str, list[str]]] = []
    for t in treks:
        status_icon = {
            "planning": "○",
            "active": "●",
            "archived": "□",
        }.get(t.get("status", ""), "?")
        halt_marker = " [halted]" if t.get("halt") else ""
        member_count = len(t.get("members") or [])
        scope_entries = t.get("scope") or []
        scope_count = len(scope_entries)
        project_wide = [
            s.get("project") or ""
            for s in scope_entries
            if not any(s.get(k) for k in ("milestone", "operation", "task"))
        ]
        pw_marker = (
            f" [⚠ {len(project_wide)} project-wide]" if project_wide else ""
        )
        print(f"  {status_icon} {t['trek_id']:14s} {t['title'][:55]}"
              f" — {t.get('type', '?')}/{t.get('status', '?')}"
              f"{halt_marker}, {member_count}m/{scope_count}s{pw_marker}")
        if project_wide:
            project_wide_treks.append((t["trek_id"], project_wide))
    if project_wide_treks:
        print()
        print("⚠ Project-wide scope entries detected "
              "(= legacy, narrowing key 無し):")
        for trek_id, projects in project_wide_treks:
            for project in projects:
                print(f"  - {trek_id}: {project}")
        print("これらは旧 SPEC で許容されていた entry で、 "
              "新規追加は server reject されます。")
        print("対処: `beacon trek plan --remove-scope <project>` で削除し、 "
              "`beacon trek plan --add-scope <project>:<ms-id|op-id|e-id>` "
              "で narrowing 付き entry を staging してください。")


def _scope_matches_entry(scope: list[dict], project_id: str,
                         entry: dict, ms_id: str) -> bool:
    """Return True if ``entry`` (in milestone ``ms_id``) is in the trek scope.

    A scope row matches when its ``project`` matches AND any narrowing key
    (milestone / task) matches. Project-wide scope (= no narrowing) is a
    catch-all that includes every milestone/task in the project.
    """
    if not scope or not project_id:
        return False
    eid = entry.get("id", "")
    for row in scope:
        if (row.get("project") or "") != project_id:
            continue
        # Project-wide scope row → always matches.
        narrow_keys = [k for k in ("milestone", "task", "operation")
                       if row.get(k)]
        if not narrow_keys:
            return True
        if row.get("milestone") and row.get("milestone") == ms_id:
            return True
        if row.get("task") and row.get("task") == eid:
            return True
        # operation narrowing doesn't apply to milestone entries; let the
        # operation-walk pick it up instead.
    return False


def _scope_matches_operation(scope: list[dict], project_id: str,
                             op_id: str) -> bool:
    if not scope or not project_id:
        return False
    for row in scope:
        if (row.get("project") or "") != project_id:
            continue
        if not any(row.get(k) for k in ("milestone", "task", "operation")):
            return True  # project-wide
        if row.get("operation") and row.get("operation") == op_id:
            return True
    return False


def _scope_matches_milestone(scope: list[dict], project_id: str,
                             ms_id: str) -> bool:
    if not scope or not project_id:
        return False
    for row in scope:
        if (row.get("project") or "") != project_id:
            continue
        if not any(row.get(k) for k in ("milestone", "task", "operation")):
            return True
        if row.get("milestone") and row.get("milestone") == ms_id:
            return True
    return False


def _collect_trek_local_aggregation(trek_doc: dict) -> dict:
    """Walk the current project and collect items in this trek's scope.

    Returns a dict shaped as::

        {
          "project_id": "<pid or empty>",
          "tasks_todo": [{ms_id, ms_title, id, description, status, priority}],
          "tasks_done_recent": [...],
          "commits_recent": [{ms_id, hash, summary, date}],
          "docs": [{doc_id, title, scope, milestone, trek_id}],
          "cross_project_scope": [{project, milestone/task/operation}],
        }

    Cross-project scope rows are surfaced as hints so the CLI prompts the
    user to cd into the other project; only the current project's items
    are walked here (= e-1864 CLI-side aggregation lives at the project
    grain, no remote fetch).
    """
    scope = trek_doc.get("scope") or []
    pid = _current_project_id()
    out: dict = {
        "project_id": pid,
        "tasks_todo": [],
        "tasks_done_recent": [],
        "commits_recent": [],
        "docs": [],
        "cross_project_scope": [s for s in scope
                                if (s.get("project") or "") != pid],
    }
    if not pid:
        return out
    try:
        data = load_project()
    except Exception:
        return out

    done_recent: list[dict] = []
    commits: list[dict] = []
    for ms in data.get("milestones", []) or []:
        ms_id = ms.get("id", "")
        ms_title = work_model.target_label(ms)
        for entry in ms.get("entries", []) or []:
            etype = entry.get("type", "")
            if etype == "commit":
                if _scope_matches_milestone(scope, pid, ms_id) or \
                   _scope_matches_entry(scope, pid, entry, ms_id):
                    commits.append({
                        "ms_id": ms_id,
                        "hash": (entry.get("meta") or {}).get("hash", ""),
                        "summary": entry.get("description", ""),
                        "date": entry.get("created_at", ""),
                    })
                continue
            if not _scope_matches_entry(scope, pid, entry, ms_id):
                continue
            row = {
                "ms_id": ms_id,
                "ms_title": ms_title,
                "id": entry.get("id", ""),
                "description": entry.get("description", ""),
                "status": entry.get("status", ""),
                "priority": (entry.get("meta") or {}).get("priority", ""),
                "type": etype,
            }
            if entry.get("status") == "todo":
                out["tasks_todo"].append(row)
            elif entry.get("status") == "done":
                done_recent.append(row | {"done_at": entry.get("done_at", "")})

    # Operations matched at trek scope (= ms-75 4.4 UC7-F4 ハイブリッド入口、
    # ここでは entries は別途扱わず、Operation 自体の状況を要約)
    ops_in_scope = []
    for op in data.get("operations", []) or []:
        op_id = op.get("id", "")
        if _scope_matches_operation(scope, pid, op_id):
            ops_in_scope.append({
                "id": op_id,
                "title": work_model.target_label(op),
                "status": op.get("status", ""),
            })
    out["operations"] = ops_in_scope

    # Sort recent commits / done tasks newest-first, cap to 5 each so the
    # default view stays readable. --detail / --all in the caller can lift
    # the cap if we want a full expansion (= e-1864 AC 2).
    commits.sort(key=lambda c: c.get("date", ""), reverse=True)
    done_recent.sort(key=lambda r: r.get("done_at", ""), reverse=True)
    out["commits_recent"] = commits[:5]
    out["tasks_done_recent"] = done_recent[:5]

    # Forward doc lookup (= e-1866): docs tagged with this trek_id in the
    # current project's docs/ directory. Cross-project doc lookup lives
    # behind /api/treks/{tid}/documents (cloud-mode).
    docs: list[dict] = []
    docs_dir = _get_docs_dir()
    if os.path.isdir(docs_dir):
        for fname in sorted(os.listdir(docs_dir)):
            if not fname.endswith(".md"):
                continue
            try:
                doc = _read_local_doc(os.path.join(docs_dir, fname))
            except (OSError, UnicodeDecodeError):
                continue
            if doc.get("trek_id") != trek_doc.get("trek_id"):
                continue
            docs.append({
                "doc_id": doc.get("doc_id", ""),
                "title": doc.get("title", ""),
                "scope": doc.get("scope", ""),
                "milestone": doc.get("milestone", ""),
                "updated_at": doc.get("updated_at", ""),
            })
    out["docs"] = docs
    return out


def _goal_state_status(trek_doc: dict, agg: dict) -> str:
    """Return a 1-line readable status of the trek's goal_state field.

    The criterion itself is free-form text, so we cannot programmatically
    score completion. Instead we surface a simple counter ("3 todo / 7
    done") that members can match against the text to decide whether to
    suggest archive. This is intentionally lightweight — the SPEC explicitly
    rejects "completion enforcement" as overdesign.
    """
    goal = (trek_doc.get("goal_state") or "").strip()
    if not goal:
        return ""
    todo = len(agg.get("tasks_todo") or [])
    done = len(agg.get("tasks_done_recent") or [])
    return (
        f"goal:        {goal}\n"
        f"  progress (current project): {todo} todo / {done} recently-done"
        + (" — consider `beacon trek archive` if criterion is met."
           if todo == 0 and done > 0 else "")
    )


def cmd_trek_review_verdicts():
    """Emit the verdict set a leader may pick for a leader_review target.

    ms-128 方針6 / e-4374 (AX/maintainability review PR#545) — the single
    source of truth for "which verdicts apply" is ``trek.leader_review_verdict_set``
    (branches on the halt_reason tag: completion vs halt-rescue). The
    /beacon-trek-review Skill calls THIS verb and renders whatever it returns,
    instead of hand-copying the verdict lists into prose (which would silently
    drift from the lib). Reads BEACON_TREK_ID / BEACON_TREK_TASK_ID; always
    emits JSON (the Skill parses it).
    """
    import trek as _trek

    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    task_id = os.environ.get("BEACON_TREK_TASK_ID", "").strip()
    if not trek_id or not task_id:
        print("Error: trek_id and task_id are required", file=sys.stderr)
        sys.exit(1)
    try:
        t = get_store().get_trek(trek_id)
    except ValueError:
        print(f"Error: trek {trek_id} not found", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    result = _trek.leader_review_verdict_set(t, task_id)
    print(json.dumps(result, ensure_ascii=False))


def cmd_trek_show():
    """Show a single trek by id with task / commit / doc aggregation.

    Reads from env:
      BEACON_TREK_ID  required
      BEACON_JSON     "1" → emit json (= raw trek doc + ``aggregation`` key)
      BEACON_ALL      "1" → uncap recent lists (= --detail equivalent)

    ms-75 / e-1864: human output now surfaces the trek's scoped tasks /
    commits / docs from the *current* project so a CLI-driven workflow can
    see Trek-wide progress without bouncing to the Web UI. Cross-project
    scope rows are surfaced as hints (= the user must cd into the other
    project) — the CLI intentionally does not fan out into other
    project.json files to keep aggregation cheap and unambiguous.
    """
    import trek_store

    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    expand = os.environ.get("BEACON_ALL", "") == "1"

    if not trek_id:
        print("Error: trek_id is required", file=sys.stderr)
        sys.exit(1)

    # ms-84 Phase 2: Store 経由で cloud / local を統一。 Store.get_trek は
    # ValueError on unknown / RuntimeError on transport の error contract を
    # 両 backend で共有しているため、 CLI 側はバックエンドを意識せず
    # 同じ except 分岐で扱える。
    try:
        t = get_store().get_trek(trek_id)
    except ValueError:
        print(f"Error: trek {trek_id} not found", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Build local aggregation regardless of cloud/local — the current
    # project view is always available.
    agg = _collect_trek_local_aggregation(t)

    if json_mode:
        # Emit the trek doc as-is, plus an ``aggregation`` key. Existing
        # consumers (= /beacon-trek-execute Step 1) that only read top-level
        # fields are unaffected; new consumers can opt into the aggregation
        # block by name (= forward-compatible).
        out = dict(t)
        out["aggregation"] = agg
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    halt_marker = " [HALTED]" if t.get("halt") else ""
    # ms-99 / e-2834 — quiesce marker in the header so an operator eyeing
    # a Trek immediately sees "AI 自律実行完了 (task_state_aggregate_terminal)"
    # without hunting through meta. The stamp is set by the scheduler
    # tick's quiesce branch and cleared by ``PATCH .../task-state`` when
    # a task transitions out of terminal.
    quiesced_meta = (t.get("meta") or {}).get("quiesced_at") or ""
    quiesce_marker = f" [QUIESCED @ {quiesced_meta[:19]}]" if quiesced_meta else ""
    print(f"Trek {t['trek_id']} — {t['title']}{halt_marker}{quiesce_marker}")
    print(f"  type:        {t.get('type')}")
    print(f"  status:      {t.get('status')}")
    if quiesced_meta:
        meta = t.get("meta") or {}
        print(
            f"  quiesced:    {quiesced_meta[:19]} "
            f"(reason={meta.get('quiesce_reason', '')}, "
            f"notified={bool(meta.get('quiesce_notified_at'))})"
        )
    print(f"  created:     {t.get('created_at', '')[:19]}")
    if t.get("archived_at"):
        print(f"  archived:    {t.get('archived_at', '')[:19]}")
    creator = t.get("creator_actor") or {}
    print(f"  creator:     {creator.get('email')} (user_id={creator.get('user_id')})")
    print(f"  leader sess: {t.get('leader_session_id')}")
    if t.get("description"):
        print(f"  description: {t['description']}")
    goal_line = _goal_state_status(t, agg)
    if goal_line:
        # _goal_state_status returns 1-2 lines, prefix each with two spaces
        # so it visually aligns with the other show fields.
        for line in goal_line.split("\n"):
            print(f"  {line}" if not line.startswith("  ") else line)
    members = t.get("members") or []
    print(f"  members ({len(members)}):")
    for m in members:
        joined = "joined" if m.get("joined_at") else "invited"
        print(f"    - {m.get('email')} [{m.get('role')}] ({joined})")
    scope = t.get("scope") or []
    print(f"  scope ({len(scope)}):")
    project_wide_entries: list[dict] = []
    for s in scope:
        # ms-75 / e-1864 AC 6: surface bind grain (project / project:task=eXXX /
        # project:ms=msXX / project:op=opXX) explicitly so members understand
        # whether the trek covers a whole project or a narrow item.
        narrow = [(k, v) for k, v in s.items() if k != "project"]
        if narrow:
            ref_str = ", ".join(f"{k}={v}" for k, v in narrow)
            print(f"    - {s.get('project')}:{ref_str}")
        else:
            print(f"    - {s.get('project')} (project-wide)")
            project_wide_entries.append(s)
    # ms-97 / e-2659 (AC8 + AC31): warn the operator when the trek still
    # carries grandfathered project-wide scope entries (= rows with no
    # narrowing key). New additions are server-rejected (AC7), but legacy
    # data stays readable. Surface the gap so the operator knows the trek
    # is broader than today's policy would allow.
    if project_wide_entries:
        print()
        print("  ⚠ Project-wide scope entries detected "
              "(= legacy, narrowing key 無し):")
        for s in project_wide_entries:
            print(f"    - {s.get('project')}")
        print("  これらは旧 SPEC で許容されていた entry で、 "
              "新規追加は server reject されます。")
        print("  対処: `beacon trek plan --remove-scope <project>` で削除し、 "
              "`beacon trek plan --add-scope <project>:<ms-id|op-id|e-id>` "
              "で narrowing 付き entry を staging してください。")
    if t.get("halt"):
        h = t["halt"]
        print(f"  halt: at={h.get('issued_at')} by={h.get('issued_by_session_id')}"
              + (f" reason={h.get('reason')}" if h.get("reason") else ""))

    # ms-75 / e-1864: aggregation sections. Cross-project scope rows can't
    # be expanded locally, so we surface them as hints rather than silently
    # omitting items the user expects to see.
    if agg.get("cross_project_scope"):
        print()
        print(f"  cross-project scope (not aggregated locally; cd into the "
              f"other project for detail):")
        for s in agg["cross_project_scope"]:
            narrow = [(k, v) for k, v in s.items() if k != "project"]
            if narrow:
                ref_str = ", ".join(f"{k}={v}" for k, v in narrow)
                print(f"    - {s.get('project')}:{ref_str}")
            else:
                print(f"    - {s.get('project')} (project-wide)")

    todos = agg.get("tasks_todo") or []
    if todos:
        print()
        print(f"  open tasks in scope ({len(todos)}):")
        shown = todos if expand else todos[:10]
        for row in shown:
            pri = f" [{row['priority']}]" if row.get("priority") else ""
            print(f"    ○ [{row['id']}]{pri} {row['description'][:80]}")
        if not expand and len(todos) > len(shown):
            print(f"    … {len(todos) - len(shown)} more (pass --all to "
                  f"expand)")

    done_recent = agg.get("tasks_done_recent") or []
    if done_recent:
        print()
        print(f"  recently done in scope ({len(done_recent)} shown):")
        for row in done_recent:
            print(f"    ● [{row['id']}] {row['description'][:80]}")

    ops_in_scope = agg.get("operations") or []
    if ops_in_scope:
        print()
        print(f"  Operations in scope ({len(ops_in_scope)}):")
        for op in ops_in_scope:
            print(f"    - [{op['id']}] {op['title'][:60]} ({op['status']})")

    commits = agg.get("commits_recent") or []
    if commits:
        print()
        print(f"  recent commits in scope ({len(commits)} shown):")
        for c in commits:
            short = (c.get("hash") or "")[:8]
            print(f"    {short:8s} {c['summary'][:80]}")

    docs = agg.get("docs") or []
    if docs:
        print()
        print(f"  trek docs ({len(docs)}):")
        scope_icons = {"core": "*", "spec": "+", "memo": "-",
                       "retro": "~", "report": "!"}
        for d in docs:
            icon = scope_icons.get(d.get("scope") or "memo", "?")
            print(f"    {icon} [{d.get('scope', 'memo')}] "
                  f"{d['doc_id']}: {d['title'][:60]}")


def cmd_trek_timeline():
    """Show a chronological timeline of trek-scoped events (ms-75 / e-1867).

    Combines (a) trek lifecycle events (= created / status / halt / scope
    changes / members) reconstructed from the trek doc's timestamps,
    (b) scope-matching commits and task done events from the current
    project, (c) trek-scoped doc additions, and (d) Trek-scope DM events
    if a local bus_log file is available. Cross-project events live behind
    the Tauri / Web UI (ms-72) and are surfaced here only as a one-line
    hint pointing to the trek_id.

    Reads from env:
      BEACON_TREK_ID  required
      BEACON_JSON     "1" → emit json (list of events, newest first)
      BEACON_LIMIT    integer (default 50) — cap event count
    """
    import trek_store

    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    try:
        limit = int(os.environ.get("BEACON_LIMIT", "50") or "50")
    except ValueError:
        limit = 50

    if not trek_id:
        print("Error: trek_id is required", file=sys.stderr)
        sys.exit(1)

    # ms-84 Phase 2 (e-2036): Store.get_trek unifies the cloud / local
    # branch. ValueError on not-found, RuntimeError on auth / transport
    # propagates as the original cloud branch behavior.
    try:
        t = get_store().get_trek(trek_id)
    except ValueError:
        print(f"Error: trek {trek_id} not found", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    events: list[dict] = []

    # (a) trek lifecycle.
    if t.get("created_at"):
        events.append({"at": t["created_at"], "kind": "trek_created",
                       "summary": f"trek {trek_id} created"})
    if t.get("archived_at"):
        events.append({"at": t["archived_at"], "kind": "trek_archived",
                       "summary": "trek archived"})
    for m in t.get("members") or []:
        if m.get("invited_at"):
            events.append({
                "at": m["invited_at"], "kind": "member_invited",
                "summary": f"invited {m.get('email')} [{m.get('role')}]",
            })
        if m.get("joined_at"):
            events.append({
                "at": m["joined_at"], "kind": "member_joined",
                "summary": f"joined {m.get('email')} [{m.get('role')}]",
            })
    halt = t.get("halt") or {}
    if halt.get("issued_at"):
        reason = halt.get("reason") or "(no reason)"
        events.append({
            "at": halt["issued_at"], "kind": "halt_engaged",
            "summary": f"halt engaged: {reason}",
        })

    # (b) scoped commits + done tasks from current project.
    pid = _current_project_id()
    if pid:
        try:
            data = load_project()
        except Exception:
            data = {}
        scope = t.get("scope") or []
        for ms in data.get("milestones", []) or []:
            ms_id = ms.get("id", "")
            for entry in ms.get("entries", []) or []:
                etype = entry.get("type", "")
                if etype == "commit":
                    if not (_scope_matches_milestone(scope, pid, ms_id)
                            or _scope_matches_entry(scope, pid, entry, ms_id)):
                        continue
                    events.append({
                        "at": entry.get("created_at", ""),
                        "kind": "commit",
                        "summary": (
                            f"[{ms_id}] {entry.get('description', '')[:80]}"
                        ),
                    })
                    continue
                if not _scope_matches_entry(scope, pid, entry, ms_id):
                    continue
                if entry.get("done_at"):
                    events.append({
                        "at": entry["done_at"],
                        "kind": "task_done",
                        "summary": (
                            f"[{entry.get('id', '')}] "
                            f"{entry.get('description', '')[:80]}"
                        ),
                    })

    # (c) trek-scoped docs.
    docs_dir = _get_docs_dir()
    if os.path.isdir(docs_dir):
        for fname in sorted(os.listdir(docs_dir)):
            if not fname.endswith(".md"):
                continue
            try:
                doc = _read_local_doc(os.path.join(docs_dir, fname))
            except (OSError, UnicodeDecodeError):
                continue
            if doc.get("trek_id") != trek_id:
                continue
            events.append({
                "at": doc.get("updated_at", ""),
                "kind": "doc",
                "summary": (
                    f"[{doc.get('scope', 'memo')}] {doc.get('doc_id', '')}: "
                    f"{doc.get('title', '')[:70]}"
                ),
            })

    # Newest first; cap.
    events.sort(key=lambda e: e.get("at", ""), reverse=True)
    events = events[:max(limit, 0)] if limit > 0 else events

    if json_mode:
        print(json.dumps(events, ensure_ascii=False))
        return

    if not events:
        print(f"Trek {trek_id} timeline: (no events found in current "
              f"project; cross-project events are visible via the Web / "
              f"Tauri UI)")
        return

    print(f"Trek {trek_id} timeline ({len(events)} events shown, newest "
          f"first):")
    kind_icons = {
        "trek_created": "*", "trek_archived": "!", "member_invited": "+",
        "member_joined": "+", "halt_engaged": "!", "commit": ">",
        "task_done": "●", "doc": "-",
    }
    for ev in events:
        when = (ev.get("at") or "")[:19]
        icon = kind_icons.get(ev.get("kind", ""), "?")
        print(f"  {when}  {icon} [{ev.get('kind')}] {ev.get('summary', '')}")


def _trek_transition(trek_id: str, to_status: str):
    """Helper: validate + apply a state transition. Returns the updated trek.

    Local mode only. Cloud mode dispatches via the dedicated start/archive
    endpoints on the server (= caller branches before invoking this).
    """
    import trek
    import trek_store

    t = trek_store.load_trek(trek_id)
    if t is None:
        print(f"Error: trek {trek_id} not found", file=sys.stderr)
        sys.exit(1)
    cur = t.get("status", "")
    try:
        trek.validate_transition(cur, to_status)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    updates = {
        "status": to_status,
        "updated_at": trek.utcnow_iso(),
    }
    if to_status == "archived":
        updates["archived_at"] = updates["updated_at"]
    return trek_store.update_trek(trek_id, updates=updates)


def cmd_trek_start():
    """Transition trek planning → active."""
    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not trek_id:
        print("Error: trek_id is required", file=sys.stderr)
        sys.exit(1)
    if _is_cloud_mode():
        try:
            client, _config = _get_api_client()
            t = client.start_trek(trek_id)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        t = _trek_transition(trek_id, "active")
    if json_mode:
        print(json.dumps(t, ensure_ascii=False))
    else:
        print(f"Started trek {t['trek_id']} (status: active)")


def cmd_trek_archive():
    """Transition trek → archived (terminal)."""
    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not trek_id:
        print("Error: trek_id is required", file=sys.stderr)
        sys.exit(1)
    if _is_cloud_mode():
        try:
            client, _config = _get_api_client()
            t = client.archive_trek(trek_id)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        t = _trek_transition(trek_id, "archived")
    if json_mode:
        print(json.dumps(t, ensure_ascii=False))
    else:
        print(f"Archived trek {t['trek_id']} — recorded for posterity, "
              "再開したい時は新 trek 起票")


def cmd_trek_invite():
    """Invite a user (by email) to a trek (= add to members[] with joined_at='').

    Env:
      BEACON_TREK_ID       (required) trek to invite into
      BEACON_TREK_ACTOR    (required) invitee's email
      BEACON_TREK_NOTIFY   "1" → also send a live DM (= e-1662 で実装、
                           現在は acknowledged but no-op)
      BEACON_USER_EMAIL    inviter's email (= invited_by, defaults to whoami)
      BEACON_JSON          "1" → json output
    """
    import trek
    import trek_store

    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    actor_email = os.environ.get("BEACON_TREK_ACTOR", "").strip()
    notify = os.environ.get("BEACON_TREK_NOTIFY", "") == "1"
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not trek_id:
        print("Error: trek_id is required", file=sys.stderr)
        sys.exit(1)
    if not actor_email:
        print("Error: --actor <email> is required", file=sys.stderr)
        sys.exit(1)

    inviter_user_id, _, _ = _resolve_creator_identity()

    if _is_cloud_mode():
        # Cloud path: server resolves the invitee's user_id via
        # find_user_by_email + records inviter from auth token.
        try:
            client, _config = _get_api_client()
            t = client.invite_trek_member(trek_id, actor_email)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        t = trek_store.load_trek(trek_id)
        if t is None:
            print(f"Error: trek {trek_id} not found", file=sys.stderr)
            sys.exit(1)
        # Local mode identity: user_id = email (cloud mode resolves properly
        # via auth in e-1656). When the invitee later runs `beacon trek join`,
        # their BEACON_USER_ID must match — easiest path is to also use email
        # there.
        invitee_user_id = actor_email
        try:
            trek.add_invitation(
                t, user_id=invitee_user_id, email=actor_email,
                invited_by_user_id=inviter_user_id,
            )
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        trek_store.save_trek(t)

    if json_mode:
        print(json.dumps(t, ensure_ascii=False))
    else:
        member_count = len(t.get("members") or [])
        print(f"Invited {actor_email} to trek {trek_id} "
              f"(members: {member_count})")
        if notify:
            # e-1662 で bus DM 経路を実装予定。現在は --notify を受け付ける
            # だけで、DM は飛ばない (= invitee が trigger check / session-start
            # で気付く流れ default)。
            print("  (--notify is acknowledged but live DM is not implemented "
                  "yet — invitee will see the invitation via trigger check; "
                  "live DM lands in e-1662)")


def _arm_for_trek(trek_id: str) -> dict:
    """Run the 3 auto-arm actions for a freshly-joined Trek.

    ms-75 / e-2047 AC 1 — pre-armed Trek participation: a session that
    joins a Trek should be ready to act on scope-internal DMs without
    requiring the user to remember to add channels + grant budget + start
    the /beacon-bus-armed Skill. This helper performs the first two
    structurally (= file writes) and surfaces a hint for the third (=
    Skill invocation belongs to the AI side, not CLI).

    Idempotency:
      * channel allowlist add is idempotent — re-running for the same
        trek is a no-op for channels already present.
      * budget set is unconditional (= refresh to default). Re-joining a
        trek effectively re-arms; this matches the user intent (= "I am
        rejoining this work, give me a fresh runway").

    Returns a dict with the actions taken so callers can render an audit
    summary or emit JSON without re-reading the files.
    """
    import datetime
    data = load_project()
    channels = _bus_auto_execute_channels(data)
    added: list[str] = []
    for ch in TREK_AUTO_ARM_CHANNELS:
        if ch not in channels:
            channels.append(ch)
            added.append(ch)
    if added:
        data["bus_auto_execute_channels"] = channels
        save_project(data, op={
            "op": "trek_auto_arm",
            "trek_id": trek_id,
            "channels_added": added,
        })
        _mirror_auto_execute_channels_to_local(channels)
    budget_data = {
        "total": TREK_AUTO_ARM_DEFAULT_BUDGET,
        "used": 0,
        "granted_at": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"),
        "channels": [],
        "trek_id": trek_id,  # audit marker (= auto-arm source)
    }
    _write_bus_budget(budget_data)
    return {
        "trek_id": trek_id,
        "channels": list(channels),
        "channels_added": added,
        "budget_turns": TREK_AUTO_ARM_DEFAULT_BUDGET,
    }


def _build_trek_join_consent_explanation(trek_id: str, email: str) -> str:
    """Build the 4-section consent explanation text (ms-92 / e-2182).

    Sections (= AC #1 of e-2182, mirroring CORE doc trek-positioning
    `b1XOKXQeC0JXaKkO0CRt`「缶詰の徹夜作業部屋」 vocabulary):

      (a) **Trek とは何か** — what the user is opting into in 1 line
      (b) **委譲する権限** — concrete actions AI gains permission for,
          each with a one-line example so the user can recognise the
          shape of the autonomy being granted
      (c) **user 確認境界** — concrete things AI still must not do
          (= deploy / release / scope-out / 不可逆 actions / PR merge),
          so the user knows where their judgment is still required
      (d) **撤回方法** — how to leave the Trek, what happens after

    Split into a builder so cloud / local paths share the same text
    (= AC #6) and tests can assert the 4 sections via plain string
    search without mocking stdin / stderr.
    """
    return (
        f"\n─── Trek 参加同意 (ms-92 e-2182) ─────────────────────\n"
        f"trek_id:    {trek_id}\n"
        f"joining as: {email}\n"
        f"\n"
        f"(a) Trek とは何か\n"
        f"   Trek (= 缶詰の徹夜作業部屋) は 「事前承認スコープを持つ自律実行の作業空間」 です。\n"
        f"   一度参加すると、 scope 内の DM や action は AI 判断で進みます (= turn 制限なし)。\n"
        f"   詳細: CORE doc trek-positioning (b1XOKXQeC0JXaKkO0CRt)。\n"
        f"\n"
        f"(b) 参加で委譲する権限 (= 個別承認なしで AI が実行できるようになる行為)\n"
        f"   - scope 内 DM が blanket 自動承認 (= 一括許可) で配信される\n"
        f"     例: 別 session からの「次やって」 DM が user 確認なく届く (= ms-70 / e-1854 blanket bypass)\n"
        f"   - executor (= 実行担当 session) が working / done / waiting-review を自分で宣言できる\n"
        f"     例: subagent が task 完了を自己判断で stamp、 leader が後でまとめて review\n"
        f"   - leader が PR (= プルリクエスト) の approve / reject を AI 自律で進められる\n"
        f"     例: PR 内容が intent (= 目的) と整合していれば AI 単独で approve、 merge は別境界 (= (c) 参照)\n"
        f"   - server-side scheduler が定期的に「次やって」 progress-check を push してくる\n"
        f"     例: trek-progress-check / trek-trigger / trek-task-review channel が autonomous loop に乗る\n"
        f"\n"
        f"(c) user 確認境界 (= AI が touched せず、 必ず user に escalate される領域)\n"
        f"   - deploy (= 本番への配置) / release (= リリース ceremony) は必ず user 承認\n"
        f"   - Trek scope 外の action (= 別 project / 別 MS への直接 write) は user 確認必須\n"
        f"   - 不可逆 action (= git force-push / hard delete / 外部 email 送信 等) は user_review に forward、 AI 単独 NG\n"
        f"   - 個別 PR の merge は AI 自律 NG。 Trek 終結時に user 1 confirm で集約承認\n"
        f"     (= e-2169 で確立した 「approve = AI / merge = Trek 単位 user / release = user」 の 3 段境界)\n"
        f"\n"
        f"(d) 撤回方法\n"
        f"   いつでも `beacon trek leave {trek_id}` で抜けられます。\n"
        f"   leave 後は blanket 自動承認が解除され、 以後の DM は通常の user 確認経路に戻ります。\n"
        f"   leader role を持っている場合は先に `beacon trek transfer-leader {trek_id} --to <session_id>` で\n"
        f"   後任を立ててから leave してください (= last-leader 抜けは server が 400 で reject)。\n"
    )


def _trek_join_consent_gate(trek_id: str, email: str, *, json_mode: bool) -> None:
    """Gate ``beacon trek join`` on per-session 明示同意 (ms-88 / e-2090).

    Behavior:
    - TTY stdin → prompt for the literal phrase ``I UNDERSTAND``. Mismatch =
      abort with exit 1 and a one-line explanation. Case-sensitive (= a
      thoughtful enough action to require precise typing, not a reflexive y).
    - Non-TTY stdin (= bot / CI / Skill pipe) → refuse and instruct caller
      to use ``--i-understand-the-implications`` flag explicitly. We never
      auto-accept on non-TTY because that path is exactly the silent bypass
      this gate exists to close.
    - ``json_mode`` is irrelevant to the gate itself but affects the abort
      payload (= keep stderr human-readable either way; the gate is a UX
      checkpoint, not a JSON API).

    The explanation text is built by ``_build_trek_join_consent_explanation``
    (ms-92 / e-2182) so cloud / local paths share the same 4-section
    structure and tests can assert section presence without driving
    stdin / stderr.
    """
    explanation = _build_trek_join_consent_explanation(trek_id, email)

    if not sys.stdin.isatty():
        # Non-TTY (= bot / CI / Skill pipe) は typed prompt を取れない。
        # silent auto-accept は本 gate の趣旨に反するので明示 flag を要求。
        sys.stderr.write(explanation)
        sys.stderr.write(
            "\n─── 自動化 / bot 経路 ────────────────────────────────\n"
            "非 TTY 経由のため typed-ack を取れません。 自動化 / bot 経路で\n"
            "参加する場合は `--i-understand-the-implications` を明示的に渡してください\n"
            "(= flag enforcement、 e-2090 で land した forcing function)。\n"
        )
        sys.exit(1)

    sys.stderr.write(explanation)
    sys.stderr.write(
        f"\n─── 同意確認 ───────────────────────────────────────\n"
        f"上記 (a)-(d) を理解したうえで参加する場合は次のフレーズを正確に入力してください\n"
        f"(case-sensitive、 余分な空白なし):\n"
        f"   {TREK_JOIN_CONSENT_PHRASE}\n"
        f"\n> "
    )
    sys.stderr.flush()
    try:
        typed = input("").strip()
    except (EOFError, KeyboardInterrupt):
        sys.stderr.write("\n  aborted by user — Trek 参加を中止しました。\n")
        sys.exit(1)
    if typed != TREK_JOIN_CONSENT_PHRASE:
        sys.stderr.write(
            f"\n  入力 {typed!r} が期待値と一致しません — Trek 参加を中止しました。\n"
            f"  必要なら再度 `beacon trek join {trek_id}` から実行してください。\n"
        )
        sys.exit(1)


def cmd_trek_join():
    """Accept an invitation (= set member.joined_at = now for the current user).

    ms-75 / e-2047 — by default also auto-arms the session for the joined
    Trek: adds trek-progress-check / trek-trigger / trek-task-review to
    bus_auto_execute_channels (idempotent), sets the outbound-send budget
    to 20 turns, and prints a hint to start the /beacon-bus-armed Skill.
    Opt-out: ``BEACON_TREK_NO_ARM=1`` (mapped from ``--no-arm`` at the
    bash / dispatch.py shim).

    Env:
      BEACON_TREK_ID     (required)
      BEACON_USER_EMAIL  (required, matched against the invitee's email)
      BEACON_USER_ID     inviter's recorded user_id (fallback: whoami).
                         Used as the local-mode identity match.
      BEACON_SESSION_ID  (informational, recorded if leader_session_id is empty)
      BEACON_TREK_NO_ARM "1" → skip auto-arm post-join
      BEACON_JSON        "1" → json output
    """
    import trek
    import trek_store

    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    no_arm = os.environ.get("BEACON_TREK_NO_ARM", "") == "1"
    consent_ack = os.environ.get("BEACON_TREK_CONSENT_ACK", "") == "1"
    if not trek_id:
        print("Error: trek_id is required", file=sys.stderr)
        sys.exit(1)

    # ms-97 / e-2694 dogfood fix: BEACON_SESSION_ID env unset is the
    # default for many user shells (= the env var is only set by the
    # bclaude wrapper / dispatch shim). Without auto-derive, the server
    # sees ``X-Beacon-Session: <empty>`` on the join HTTP call, falls
    # into the pre-A path, and silently no-ops on a phase A+ trek (=
    # leader stamp only, no member expansion). Resolve eagerly here so
    # the env var is set for both the local-mode local writer AND the
    # downstream api_client._request, restoring a single source of truth
    # for the joining session id.
    if not os.environ.get("BEACON_SESSION_ID", "").strip():
        derived_sid = _resolve_session_id()
        if derived_sid:
            os.environ["BEACON_SESSION_ID"] = derived_sid

    user_id, email, _ = _resolve_creator_identity()
    if not email:
        print(
            "Error: BEACON_USER_EMAIL is required to join a trek",
            file=sys.stderr,
        )
        sys.exit(1)

    # ms-88 / e-2090 — per-session 明示同意 gate。 Trek 参加 = scope 内 DM blanket
    # 自動承認 (= ms-70 / e-1854) + autonomous loop 入場の合算で turn 制限なく
    # AI を動かす権限委譲。 user が consequence を理解せず参加する構造的危険を
    # 構造的に塞ぐため、 typed-ack または明示 flag を要求。
    # bypass 経路: --i-understand-the-implications flag (dispatch.py で
    # BEACON_TREK_CONSENT_ACK=1 にマップされる)、 または BEACON_TREK_CONSENT_ACK=1
    # 環境変数 (= テスト fixture / 自動化用)。
    if not consent_ack:
        _trek_join_consent_gate(trek_id, email, json_mode=json_mode)

    if _is_cloud_mode():
        # Cloud path: server identifies the joiner from auth token (no
        # email lookup needed). Non-invited callers get 403.
        try:
            client, _config = _get_api_client()
            t = client.join_trek(trek_id)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        t = trek_store.load_trek(trek_id)
        if t is None:
            print(f"Error: trek {trek_id} not found", file=sys.stderr)
            sys.exit(1)
        # Local mode identity match: prefer email lookup first (= simplest path
        # for the invitee who is told "look for an invitation to email X"); fall
        # back to user_id lookup. Use the looked-up member's user_id for the
        # actual accept call.
        # ms-86 / e-2225 — pass BEACON_SESSION_ID so the join writes a
        # session_history entry. Empty session_id is tolerated by
        # accept_invitation (= no-op on the history dimension).
        session_id = os.environ.get("BEACON_SESSION_ID", "").strip()
        # ms-97 / e-2658 Phase 1 (AC6) — phase A+ trek の時は、 まず
        # session_id grain で既存 member entry を探す (= 同 session の
        # 再 join idempotent path)。 見つからなければ email / user_id
        # grain で placeholder (= 未 accept invitation) 経路に落ちる。
        member = None
        if session_id and trek.is_session_id_keyed(t):
            member = trek.find_member(t, session_id=session_id)
        if member is None:
            member = trek.find_member_by_email(t, email)
        if member is None:
            member = trek.find_member(t, user_id=user_id)
        if member is None:
            print(
                f"Error: no invitation found for {email} (user_id={user_id}) "
                f"in trek {trek_id} — owner must `beacon trek invite` first",
                file=sys.stderr,
            )
            sys.exit(1)
        try:
            trek.accept_invitation(
                t, user_id=member["user_id"], session_id=session_id,
            )
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        trek_store.save_trek(t)

    arm_summary: Optional[dict] = None
    if not no_arm:
        try:
            arm_summary = _arm_for_trek(trek_id)
        except Exception as exc:
            # Best-effort: join succeeded; arm is a UX enhancement. Surface
            # the failure but do not unwind the join (= the user can re-run
            # the arm steps manually if needed).
            print(
                f"[warning] join succeeded but auto-arm failed: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    # ms-97 / Phase 6 (AC15) — render accident-time leader-candidate notice.
    # The cloud path may already surface this via the server response key
    # ``leader_candidate_notice``; if present we honor that text verbatim
    # (= avoids wording drift between client / server). For local mode we
    # derive the notice locally from the live trek doc.
    notice_text = ""
    if isinstance(t, dict) and t.get("leader_candidate_notice"):
        notice_text = str(t["leader_candidate_notice"])
    else:
        try:
            notice_text = trek.build_leader_candidate_notice(t)
        except Exception:
            notice_text = ""

    if json_mode:
        # Keep the trek doc as the top-level shape so existing consumers
        # (= tests / Skill bodies parsing the trek doc directly) keep
        # working. The arm summary rides on a meta key (= `_arm`, the
        # underscore prefix signals "extra context, not part of the trek
        # schema").
        out = dict(t)
        if arm_summary is not None:
            out["_arm"] = arm_summary
        elif no_arm:
            out["_arm"] = {"skipped": True, "reason": "--no-arm"}
        if notice_text:
            out["leader_candidate_notice"] = notice_text
        print(json.dumps(out, ensure_ascii=False))
    else:
        print(f"Joined trek {trek_id} as {email}")
        if arm_summary is not None:
            added = arm_summary.get("channels_added") or []
            if added:
                print(
                    f"  auto-arm: added {len(added)} channel(s) to "
                    f"bus_auto_execute_channels: {', '.join(added)}"
                )
            else:
                print(
                    "  auto-arm: bus_auto_execute_channels already covers "
                    f"trek channels ({', '.join(TREK_AUTO_ARM_CHANNELS)})"
                )
            print(
                f"  auto-arm: budget granted "
                f"{arm_summary['budget_turns']} outbound sends "
                f"(refresh: `beacon bus budget grant --turns N`)"
            )
            print(
                "  next step: start the autonomous loop in this session via "
                "`/beacon-bus-armed` Skill so trek-progress-check events "
                "wake the executor without a user prompt."
            )
            print(
                "  opt-out: re-run with `--no-arm` to skip auto-arm "
                "(= only join, no channel / budget changes)."
            )
        elif no_arm:
            print(
                "  auto-arm skipped (--no-arm). "
                "Run `beacon bus auto-execute add --channel trek-progress-check` "
                "and `beacon bus budget grant --turns 20` manually to arm later."
            )

        # ms-97 / Phase 6 (AC15) — accident-time leader-candidate pre-notice.
        # Surface unconditionally at the tail of the human output so the
        # invitee sees it on the same screen as the join confirmation
        # (= no extra command, no hidden read).
        if notice_text:
            print()
            print(notice_text)


def cmd_trek_stop():
    """Engage the Andon cord (= set the trek's halt field).

    Env:
      BEACON_TREK_ID    (required)
      BEACON_TREK_REASON optional human-readable reason
      BEACON_SESSION_ID (required, recorded as issued_by_session_id)
      BEACON_JSON       "1" → json output
    """
    import trek
    import trek_store

    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    reason = os.environ.get("BEACON_TREK_REASON", "")
    session_id = os.environ.get("BEACON_SESSION_ID", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not trek_id:
        print("Error: trek_id is required", file=sys.stderr)
        sys.exit(1)
    if not session_id:
        print(
            "Error: BEACON_SESSION_ID is required (= recorded as the session "
            "that pulled the Andon cord)",
            file=sys.stderr,
        )
        sys.exit(1)

    if _is_cloud_mode():
        try:
            client, _config = _get_api_client()
            t = client.set_trek_halt(
                trek_id, issued_by_session_id=session_id, reason=reason,
            )
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        t = trek_store.load_trek(trek_id)
        if t is None:
            print(f"Error: trek {trek_id} not found", file=sys.stderr)
            sys.exit(1)
        try:
            trek.set_halt(t, issued_by_session_id=session_id, reason=reason)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        trek_store.save_trek(t)

    if json_mode:
        print(json.dumps(t, ensure_ascii=False))
    else:
        suffix = f" — {reason}" if reason else ""
        print(f"STOP signal raised on trek {trek_id} by {session_id}{suffix}")
        print("  All participating sessions will halt their autonomous work.")
        print("  Resume with: beacon trek resume " + trek_id)


def cmd_trek_resume():
    """Clear the halt signal. Idempotent.

    Env:
      BEACON_TREK_ID  (required)
      BEACON_JSON     "1" → json output
    """
    import trek
    import trek_store

    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not trek_id:
        print("Error: trek_id is required", file=sys.stderr)
        sys.exit(1)

    if _is_cloud_mode():
        # Server idempotently clears halt; we cannot know "was it halted"
        # from the post-clear response, so the message just confirms the
        # outcome.
        try:
            client, _config = _get_api_client()
            t = client.clear_trek_halt(trek_id)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        if json_mode:
            print(json.dumps(t, ensure_ascii=False))
        else:
            print(f"Resumed trek {trek_id} — sessions can continue work")
        return

    t = trek_store.load_trek(trek_id)
    if t is None:
        print(f"Error: trek {trek_id} not found", file=sys.stderr)
        sys.exit(1)
    had_halt = bool(t.get("halt"))
    trek.clear_halt(t)
    trek_store.save_trek(t)

    if json_mode:
        print(json.dumps(t, ensure_ascii=False))
    else:
        if had_halt:
            print(f"Resumed trek {trek_id} — sessions can continue work")
        else:
            print(f"Trek {trek_id} was not halted (no-op)")


def cmd_trek_pulse_ack():
    """Self-report /beacon-trek-pulse Skill invocation (ms-88 / e-2106).

    Layer 2 (= observability) of the 3-layer trek autonomy harness. The
    Skill calls this as Step 3 (after picking a choice) so the server has
    ground truth that the Skill actually fired in response to a scheduler
    tick. Without this self-report, the server can only infer compliance
    from indirect signals (= commit / DM activity), and dogfood (=
    tk-40b0b27c) showed those signals are insufficient.

    Env:
      BEACON_TREK_ID            (required)
      BEACON_SESSION_ID         (required) — keyed in pulse_acks[]
      BEACON_TREK_PICKED_CHOICE optional — 5-choice token: 'terminal' /
                                'continue' / 'dm-leader' / 'dm-peer' (ms-88
                                / e-2140) / 'no-op' / '' (= 空文字 legacy)
      BEACON_TREK_NOTE          optional short context (= 200 char cap)
      BEACON_TREK_STATE_SUMMARY    optional 1-line state snapshot (= ms-92 / e-2165, ≤100 chars)
      BEACON_TREK_BLOCKERS         optional newline-separated blocker list (= ms-92 / e-2165, ≤3 items × ≤200 chars)
      BEACON_TREK_NEEDS_LEADER     "1" → flag the pulse as needs_leader_judgment (= ms-92 / e-2165)
      BEACON_TREK_TIME_ON_TASK     optional integer seconds on current task (= ms-92 / e-2165, default 0)
      BEACON_JSON               "1" → json output
    """
    import trek
    import trek_store

    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    session_id = os.environ.get("BEACON_SESSION_ID", "").strip()
    picked_choice = os.environ.get("BEACON_TREK_PICKED_CHOICE", "").strip()
    note = os.environ.get("BEACON_TREK_NOTE", "")
    # ms-92 / e-2165 — structured fields. Newline-separated blockers
    # match the CLI shape "--blocker A --blocker B" expanding into a
    # bash array joined with \n at the dispatcher boundary.
    state_summary = os.environ.get("BEACON_TREK_STATE_SUMMARY", "")
    blockers_raw = os.environ.get("BEACON_TREK_BLOCKERS", "")
    blockers = [
        b for b in (blockers_raw or "").split("\n") if b.strip()
    ]
    needs_leader = os.environ.get("BEACON_TREK_NEEDS_LEADER", "") == "1"
    try:
        time_on_task = int(os.environ.get("BEACON_TREK_TIME_ON_TASK", "0") or 0)
    except ValueError:
        time_on_task = 0
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not trek_id:
        print("Error: trek_id is required", file=sys.stderr)
        sys.exit(1)
    if not session_id:
        print(
            "Error: BEACON_SESSION_ID is required (= recorded as the "
            "pulse-ack source)",
            file=sys.stderr,
        )
        sys.exit(1)

    if _is_cloud_mode():
        try:
            client, _config = _get_api_client()
            entry = client.pulse_ack_trek(
                trek_id, session_id=session_id,
                picked_choice=picked_choice, note=note,
                state_summary=state_summary,
                blockers=blockers,
                needs_leader_judgment=needs_leader,
                time_on_task_seconds=time_on_task,
            )
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        if json_mode:
            print(json.dumps(entry, ensure_ascii=False))
        else:
            total = entry.get("total_acks", 0)
            last = entry.get("last_pulse_ack_at", "")
            choice = entry.get("last_picked_choice", "")
            print(
                f"pulse-ack recorded: trek={trek_id} session={session_id} "
                f"total_acks={total} last_choice={choice or '(unset)'} "
                f"last_at={last}"
            )
        return

    # Local mode: directly mutate trek_doc.
    t = trek_store.load_trek(trek_id)
    if t is None:
        print(f"Error: trek {trek_id} not found", file=sys.stderr)
        sys.exit(1)
    try:
        trek.record_pulse_ack(
            t, session_id=session_id,
            picked_choice=picked_choice, note=note,
            state_summary=state_summary,
            blockers=blockers,
            needs_leader_judgment=needs_leader,
            time_on_task_seconds=time_on_task,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    trek_store.save_trek(t)
    entry = (t.get("pulse_acks") or {}).get(session_id) or {}
    if json_mode:
        print(json.dumps(entry, ensure_ascii=False))
    else:
        total = entry.get("total_acks", 0)
        last = entry.get("last_pulse_ack_at", "")
        choice = entry.get("last_picked_choice", "")
        print(
            f"pulse-ack recorded: trek={trek_id} session={session_id} "
            f"total_acks={total} last_choice={choice or '(unset)'} "
            f"last_at={last}"
        )


def cmd_trek_kickoff():
    """Mark Kickoff Ritual completion for the calling session (ms-88 / e-2138).

    Why this exists:
    - PR #177 (= e-2138 server side) added the schema (``kickoff_status``
      map) + endpoint (``POST /api/treks/<id>/kickoff``) + helpers
      (``trek.mark_kickoff_completed``) but landed without a CLI wrapper.
    - ``/beacon-trek-pulse`` Skill's Step 0.4 references
      ``beacon trek kickoff <trek-id>`` which would have failed with
      "command not found" — this wrapper closes that gap (= e-2139 残作業 #1,
      leader が ownership 譲渡経由で依頼)。

    Behavior:
    - **Cloud mode**: posts to ``/api/treks/<trek-id>/kickoff`` via
      ``client.kickoff_trek``. Server verifies caller is a trek member
      (user-grain) and stamps. Returns the per-session entry.
    - **Local mode**: loads trek from ``~/.beacon/treks/``, checks caller
      is a joined member (user-grain), calls ``trek.mark_kickoff_completed``,
      saves. Returns the same shape so callers can treat both modes the
      same.

    Env:
      BEACON_TREK_ID                 (required)
      BEACON_SESSION_ID              (required) — keyed in kickoff_status[]
      BEACON_TREK_KICKOFF_DM_EVENT_ID optional — bus.send 結果の event_id
                                     (= audit trace; '' で省略可)
      BEACON_JSON                    "1" → json output
    """
    import trek
    import trek_store

    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    session_id = os.environ.get("BEACON_SESSION_ID", "").strip()
    kickoff_dm_event_id = os.environ.get(
        "BEACON_TREK_KICKOFF_DM_EVENT_ID", ""
    ).strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not trek_id:
        print("Error: trek_id is required", file=sys.stderr)
        sys.exit(1)
    if not session_id:
        print(
            "Error: BEACON_SESSION_ID is required "
            "(= the session whose kickoff DM was sent)",
            file=sys.stderr,
        )
        sys.exit(1)

    user_id, email, _ = _resolve_creator_identity()

    if _is_cloud_mode():
        try:
            client, _config = _get_api_client()
            entry = client.kickoff_trek(
                trek_id,
                session_id=session_id,
                kickoff_dm_event_id=kickoff_dm_event_id,
            )
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        if json_mode:
            print(json.dumps(entry, ensure_ascii=False))
        else:
            sent_at = entry.get("sent_at") or "(empty)"
            print(
                f"Stamped trek {trek_id} kickoff for session "
                f"{session_id} (sent_at={sent_at})"
            )
        return

    # Local mode — same authorization rule as take-over: caller must be a
    # joined member at the user-grain. We don't require leader role here
    # because kickoff stamp is per-session, not a leadership transfer
    # (any joined member can declare their plan to peers).
    t = trek_store.load_trek(trek_id)
    if t is None:
        print(f"Error: trek {trek_id} not found", file=sys.stderr)
        sys.exit(1)
    member = trek.find_member_by_email(t, email) if email else None
    if member is None:
        member = trek.find_member(t, user_id=user_id)
    if member is None:
        print(
            f"Error: you are not a member of trek {trek_id} "
            f"(user_id={user_id!r}, email={email!r}). kickoff stamping "
            f"only works for joined members.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not member.get("joined_at"):
        print(
            f"Error: you have not joined trek {trek_id} yet "
            f"(invitation pending). Run `beacon trek join` first.",
            file=sys.stderr,
        )
        sys.exit(1)

    trek.mark_kickoff_completed(
        t,
        session_id=session_id,
        user_id=user_id or "",
        kickoff_dm_event_id=kickoff_dm_event_id,
    )
    trek_store.save_trek(t)
    entry = (t.get(trek.KICKOFF_HISTORY_KEY) or {}).get(session_id) or {}
    if json_mode:
        print(json.dumps(entry, ensure_ascii=False))
    else:
        sent_at = entry.get("sent_at") or "(empty)"
        print(
            f"Stamped trek {trek_id} kickoff for session "
            f"{session_id} (sent_at={sent_at})"
        )


def cmd_trek_take_over():
    """Take over leader_session_id for a fresh session of the same user (ms-88 / e-2089).

    Why this exists:
    - ``leader_session_id`` is bound to whatever session called
      ``beacon trek create`` (or the last ``transfer-leader`` target).
    - When that session dies (= Mac restart / terminal closed / fresh bclaude
      from clean env), the leader_session_id is **stale-but-non-null**:
      scheduler still fan-outs to it, DM gates still treat it as leader,
      but there's no live bclaude on the other side.
    - ``transfer-leader`` can't recover this because it requires the
      *current* leader session to authorize the swap — the dead session
      can't do that.
    - ``take-over`` is the fresh-session recovery path: any joined member
      with ``role == 'leader'`` (= user-grain check) can claim the
      ``leader_session_id`` for their **current** session in one call.

    This is *not* the same as ``join`` (= acceptance of invitation, member
    creation) or ``transfer-leader`` (= consensual handoff between live
    sessions). It is specifically for "the durable leader role is mine
    (user-grain), the live binding pointed at a dead session — bind it to
    me now".

    Env:
      BEACON_TREK_ID       (required)
      BEACON_SESSION_ID    (required) — becomes the new leader_session_id
      BEACON_JSON          "1" → json output
    """
    import trek
    import trek_store

    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    session_id = os.environ.get("BEACON_SESSION_ID", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not trek_id:
        print("Error: trek_id is required", file=sys.stderr)
        sys.exit(1)
    if not session_id:
        print(
            "Error: BEACON_SESSION_ID is required "
            "(= the calling session becomes the new leader_session_id)",
            file=sys.stderr,
        )
        sys.exit(1)

    user_id, email, _ = _resolve_creator_identity()

    if _is_cloud_mode():
        try:
            client, _config = _get_api_client()
            t = client.take_over_trek(trek_id, session_id=session_id)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        t = trek_store.load_trek(trek_id)
        if t is None:
            print(f"Error: trek {trek_id} not found", file=sys.stderr)
            sys.exit(1)

        # Local-mode authorization: caller must be a *joined leader* member
        # of this trek (user-grain). Email lookup first (= simpler UX), then
        # user_id. The point of take-over is that the calling user already
        # holds the durable leader role — we only re-bind the live session
        # pointer to a fresh session_id under that user.
        member = trek.find_member_by_email(t, email) if email else None
        if member is None:
            member = trek.find_member(t, user_id=user_id)
        if member is None:
            print(
                f"Error: you are not a member of trek {trek_id} "
                f"(user_id={user_id!r}, email={email!r}). take-over only "
                f"works for joined leader members.",
                file=sys.stderr,
            )
            sys.exit(1)
        if not member.get("joined_at"):
            print(
                f"Error: you have not joined trek {trek_id} yet "
                f"(invitation pending). Run `beacon trek join` first.",
                file=sys.stderr,
            )
            sys.exit(1)
        if member.get("role") != "leader":
            print(
                f"Error: only leader members can take over a trek "
                f"(your role: {member.get('role')!r}). Use "
                f"`beacon trek transfer-leader` from the current leader's "
                f"session if you need to be promoted first.",
                file=sys.stderr,
            )
            sys.exit(1)

        prior_leader = t.get("leader_session_id") or ""
        if prior_leader == session_id:
            # Already bound — idempotent. Print a friendly notice and exit
            # 0 so chained scripts don't trip on a re-run.
            if json_mode:
                print(json.dumps(t, ensure_ascii=False))
            else:
                print(
                    f"Leader of trek {trek_id} already bound to this session "
                    f"({session_id}); no-op."
                )
            return

        trek.transfer_leader(t, target_session_id=session_id)
        trek_store.save_trek(t)
        if json_mode:
            out = dict(t)
            out["_take_over"] = {
                "prior_leader_session_id": prior_leader,
                "new_leader_session_id": session_id,
            }
            print(json.dumps(out, ensure_ascii=False))
        else:
            print(
                f"Took over leader of trek {trek_id}: "
                f"{prior_leader or '(unset)'} → {session_id}"
            )
        return

    # Cloud mode — server already validated leader role; just print result.
    if json_mode:
        print(json.dumps(t, ensure_ascii=False))
    else:
        print(
            f"Took over leader of trek {trek_id}: "
            f"new leader_session_id = {session_id}"
        )


def cmd_trek_reconcile():
    """Reconcile Trek task_states with task pool (ms-88 / e-2167).

    2026-06-19 dogfood で観測した「task pool で done になっているのに Trek の
    task_states stamp は waiting-review / leader_review / working で残ってる」
    stuck 状態を一括修復する。

    Default は dry-run (= 変更前 diff のみ表示)。 ``--apply`` を渡すと server
    が mirror で done に書き換える (= updated_by_session_id="task-pool-mirror")。

    Env:
      BEACON_TREK_ID         (required)
      BEACON_TREK_APPLY      "1" → apply (= 実適用)、 default は dry-run
      BEACON_JSON            "1" → json output
    """
    import trek
    import trek_store

    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    apply_flag = os.environ.get("BEACON_TREK_APPLY", "") == "1"
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not trek_id:
        print("Error: trek_id is required", file=sys.stderr)
        sys.exit(1)

    if _is_cloud_mode():
        try:
            client, _config = _get_api_client()
            result = client.reconcile_trek(trek_id, apply=apply_flag)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        # Local-mode reconcile: read the trek doc and the project task pool
        # from the local filesystem, compute diff, optionally apply.
        t = trek_store.load_trek(trek_id)
        if t is None:
            print(f"Error: trek {trek_id} not found", file=sys.stderr)
            sys.exit(1)
        states = t.get("task_states") or {}
        scope_pids = [
            (s or {}).get("project") for s in (t.get("scope") or [])
            if (s or {}).get("project")
        ]
        # Local mode: scope project は cwd の project.json と一致する想定。
        # cross-project scope の局所 reconcile は cloud mode のみで完全対応。
        try:
            data = read_project()
        except Exception:
            data = None
        pool_status: dict[str, str] = {}
        if data:
            import core
            for entry_id in states.keys():
                found = core.find_entry(data, entry_id)
                if found:
                    _, _, entry, _ = found
                    pool_status[entry_id] = (entry or {}).get("status") or ""
        diff: list[dict] = []
        for entry_id, entry in states.items():
            try:
                current_state = trek.get_task_state(t, entry_id)
            except Exception:
                current_state = (entry or {}).get("state") or ""
            pool = pool_status.get(entry_id, "")
            if pool == "done" and current_state not in trek.TERMINAL_TASK_STATES:
                diff.append({
                    "entry_id": entry_id,
                    "trek_state": current_state,
                    "pool_status": pool,
                    "would_change_to": "done",
                })
        applied: list[str] = []
        if apply_flag and diff:
            import datetime
            now_iso = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            for item in diff:
                entry_id = item["entry_id"]
                existing = states.get(entry_id) or {}
                states[entry_id] = {
                    **existing,
                    "state": "done",
                    "updated_at": now_iso,
                    "last_activity_at": now_iso,
                    "updated_by_session_id": "task-pool-mirror",
                    "note": (
                        "task pool で done 化、 mirror 同期 "
                        "(= ms-88 / e-2167 reconcile)"
                    ),
                }
                applied.append(entry_id)
            t["task_states"] = states
            t["updated_at"] = now_iso
            trek_store.save_trek(t)
        result = {
            "trek_id": trek_id,
            "applied": apply_flag,
            "diff": diff,
            "applied_entry_ids": applied,
        }

    if json_mode:
        print(json.dumps(result, ensure_ascii=False))
        return

    diff = result.get("diff") or []
    applied_flag = result.get("applied")
    applied_ids = result.get("applied_entry_ids") or []
    if not diff:
        print(
            f"Trek {trek_id}: 整合済 (= task pool と Trek stamp が乖離している "
            f"task は見つかりません)"
        )
        return
    print(f"Trek {trek_id}: 乖離 {len(diff)} 件")
    for item in diff:
        print(
            f"  {item.get('entry_id')}: trek_state="
            f"{item.get('trek_state')!r} → would_change_to="
            f"{item.get('would_change_to')!r} (pool_status="
            f"{item.get('pool_status')!r})"
        )
    if applied_flag:
        print(
            f"\nApplied (= mirror で done 化): {len(applied_ids)} 件 "
            f"{applied_ids}"
        )
    else:
        print(
            "\n(dry-run、 適用するには --apply を渡してください: "
            f"`beacon trek reconcile {trek_id} --apply`)"
        )


def cmd_trek_transfer_leader():
    """Hand off leader_session_id to another session.

    Env:
      BEACON_TREK_ID       (required)
      BEACON_TREK_TO       (required) target session_id
      BEACON_JSON          "1" → json output
    """
    import trek
    import trek_store

    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    target = os.environ.get("BEACON_TREK_TO", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not trek_id:
        print("Error: trek_id is required", file=sys.stderr)
        sys.exit(1)
    if not target:
        print("Error: --to <session_id> is required", file=sys.stderr)
        sys.exit(1)

    if _is_cloud_mode():
        # Cloud mode requires caller's session as `from_session_id` for the
        # session-grain check. We use BEACON_SESSION_ID (the calling CLI's
        # session). Server also enforces user-grain leader role.
        from_session = os.environ.get("BEACON_SESSION_ID", "").strip()
        if not from_session:
            print(
                "Error: BEACON_SESSION_ID is required in cloud mode "
                "(= the caller's session; server checks it equals the "
                "current leader_session_id)",
                file=sys.stderr,
            )
            sys.exit(1)
        try:
            client, _config = _get_api_client()
            t = client.transfer_trek_leader(
                trek_id, from_session_id=from_session, to_session_id=target,
            )
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        if json_mode:
            print(json.dumps(t, ensure_ascii=False))
        else:
            print(f"Transferred leader of trek {trek_id}: "
                  f"{from_session} → {target}")
        return

    t = trek_store.load_trek(trek_id)
    if t is None:
        print(f"Error: trek {trek_id} not found", file=sys.stderr)
        sys.exit(1)

    prior_leader = t.get("leader_session_id")
    try:
        trek.transfer_leader(t, target_session_id=target)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    trek_store.save_trek(t)

    if json_mode:
        print(json.dumps(t, ensure_ascii=False))
    else:
        print(f"Transferred leader of trek {trek_id}: "
              f"{prior_leader} → {target}")


def cmd_trek_task_state():
    """Stamp Trek-internal task state (ms-75 / e-2048 + ms-88 / e-2107).

    Env:
      BEACON_TREK_ID         (required)
      BEACON_TREK_TASK_ID    (required, the entry id e-XXXX)
      BEACON_TREK_STATE      (required, one of todo/working/leader_review/
                              user_review/done; legacy `waiting-review`
                              auto-migrates to leader_review)
      BEACON_TREK_NOTE       (optional)
      BEACON_TREK_VERDICT    (optional, ms-128/e-4386 — verdict for a user_review
                              transition: approve (全 met の attainment 必須) or
                              forward-to-user (人間エスカレーション、gate 対象外)。
                              re-work は verdict でなく state=working で表す)
      BEACON_TREK_ATTAINMENT_VERDICT
                             (optional, JSON list of per-criterion verdicts:
                              [{"criterion": str, "verdict": "met"|"partial"|
                              "not-met"}, ...]; required for approve→user_review)
      BEACON_JSON            "1" → json output
    """
    import trek
    import trek_store

    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    task_id = os.environ.get("BEACON_TREK_TASK_ID", "").strip()
    state = os.environ.get("BEACON_TREK_STATE", "").strip()
    note = os.environ.get("BEACON_TREK_NOTE", "")
    verdict = os.environ.get("BEACON_TREK_VERDICT", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    # ms-128 / e-4386 — 構造化 attainment verdict を JSON で受ける。壊れた JSON は
    # 「未評価」= gate が留置する側に倒れるので silent に None 扱いにせず明示 error。
    attainment_verdict = None
    _av_raw = os.environ.get("BEACON_TREK_ATTAINMENT_VERDICT", "").strip()
    if _av_raw:
        try:
            attainment_verdict = json.loads(_av_raw)
        except (ValueError, TypeError) as e:
            print(f"Error: BEACON_TREK_ATTAINMENT_VERDICT is not valid JSON: {e}",
                  file=sys.stderr)
            sys.exit(1)

    if not trek_id:
        print("Error: trek_id is required", file=sys.stderr)
        sys.exit(1)
    if not task_id:
        print("Error: task_id is required (e.g. e-2034)", file=sys.stderr)
        sys.exit(1)
    if not state:
        print(
            "Error: state is required (one of todo/working/leader_review/"
            "user_review/done; legacy `waiting-review` も accepted、 ms-88 e-2107)",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        trek.validate_task_state(state)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    # ms-128 方針4 (e-4365) — block は `beacon trek block` (依存を張る) 経由でしか
    # 設定できない。task-state から直接 block にすると edge 無しの block が生まれ
    # 待ち先ゼロで滞留する (AX レビュー)。surface で弾く。
    if trek.migrate_legacy_task_state(state) == "block":
        print("Error: block state is set via `beacon trek block <trek> "
              "<target> --on <blocker>`, not via task-state", file=sys.stderr)
        sys.exit(1)

    if _is_cloud_mode():
        try:
            client, _config = _get_api_client()
            t = client.set_trek_task_state(
                trek_id, task_id=task_id, state=state, note=note,
                verdict=verdict, attainment_verdict=attainment_verdict,
            )
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        if json_mode:
            print(json.dumps(t, ensure_ascii=False))
        else:
            # A1 (AX review 2026-07-29): 要求した state ではなく、server が実際に
            # 保存した state を表示する。完遂ゲートが divert (user_review 要求 →
            # leader_review / working に留置) した場合、要求 state をそのまま表示すると
            # AI が「user_review 達成」と誤認する。実 state を読み、乖離時は WARN を出す。
            entry = (t.get("task_states") or {}).get(task_id) or {}
            actual = entry.get("state", state)
            requested = trek.migrate_legacy_task_state(state)
            print(f"Stamped trek {trek_id} task {task_id} state → {actual}")
            if actual != requested:
                # 原因コード + message は server が note 先頭に埋めた
                # "[attainment gate: <code>] <message>" 行から surface する
                # (AX review 2026-07-29: 留置理由 self_judgment / verdict 不足 /
                # judge 不明 を区別できないと AI が誤った回復ループに入る)。
                gate_line = (entry.get("note") or "").split("\n", 1)[0]
                print(
                    f"  ⚠ 完遂ゲートが遷移を変更: 要求 {requested} → 実際 {actual}"
                )
                if gate_line.startswith("[attainment gate:"):
                    print(f"    {gate_line}")
            # ms-97 / e-2706 — leader notify は REVIEW_TRIGGER_STATES
            # (= done / user_review / leader_review) で発火する。 CLI 表示も
            # server 側 emit 条件と一致させる (実 state で判定)。
            if actual in trek.REVIEW_TRIGGER_STATES:
                print(
                    "  Leader has been notified via trek-task-review DM "
                    "(= /beacon-trek-review surface)."
                )
        return

    t = trek_store.load_trek(trek_id)
    if t is None:
        print(f"Error: trek {trek_id} not found", file=sys.stderr)
        sys.exit(1)
    caller_sid = os.environ.get("BEACON_SESSION_ID", "")
    # ms-128 / e-4386 — 完遂ゲートを local mode でも適用する (server gate と parity)。
    # user_review へ倒す合格判定は、実行者の外の全 met attainment verdict が要る。
    effective_state = trek.migrate_legacy_task_state(state)
    from_state = trek.get_task_state(t, task_id)
    prior_stamper_sid = (
        (t.get("task_states") or {}).get(task_id) or {}
    ).get("updated_by_session_id", "")
    gate = trek.completion_gate_decision(
        effective_state=effective_state,
        from_state=from_state,
        verdict=verdict,
        caller_sid=caller_sid,
        prior_stamper_sid=prior_stamper_sid,
        attainment_verdict=attainment_verdict,
    )
    requested_state = effective_state
    gate_divert_code = ""
    gate_divert_msg = ""
    if not gate["allowed"]:
        forced = gate["forced_state"] or "leader_review"
        if forced != from_state and forced in (
            trek.VALID_TASK_STATE_TRANSITIONS.get(from_state) or ()
        ):
            state = forced
            gate_divert_code = gate["code"]
            gate_divert_msg = gate["message"]
            note = (
                f"[attainment gate: {gate['code']}] {gate['message']}\n"
                + (note or "")
            )[:500]
        else:
            print(f"Error: {gate['code']}: {gate['message']}", file=sys.stderr)
            sys.exit(1)
    try:
        trek.set_task_state(
            t,
            task_id=task_id,
            state=state,
            updated_by_session_id=caller_sid,
            note=note,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    trek_store.save_trek(t)
    if json_mode:
        print(json.dumps(t, ensure_ascii=False))
    else:
        print(
            f"Stamped trek {trek_id} task {task_id} state → {state} "
            "(local mode; review notification skipped — no bus path)"
        )
        # A1 (AX review 2026-07-29): cloud mode と同じく divert を隠さない。
        # 原因コード + message を出し、AI が誤った回復ループ (self_judgment 留置なのに
        # attainment を付け直す等) に入らないようにする。
        if gate_divert_code:
            print(
                f"  ⚠ 完遂ゲートが遷移を変更: 要求 {requested_state} → 実際 {state} "
                f"[{gate_divert_code}]"
            )
            print(f"    {gate_divert_msg}")


def cmd_trek_block():
    """Draw a blocker edge on a Trek target (ms-128 方針4 / e-4365).

    Env:
      BEACON_TREK_ID          (required)
      BEACON_TREK_TARGET_ID   (required, the dependent target)
      BEACON_TREK_BLOCKER_IDS (required, newline-joined blocker target ids)
      BEACON_TREK_NOTE        (optional)
      BEACON_JSON             "1" → json output

    Records that TARGET depends on each BLOCKER (= cannot proceed until the
    blocker is 取り込み済み) and blocks the target. Leader-only. Self-blocks and
    dependency cycles are rejected; an already-satisfied blocker is a no-op. All
    --on blockers apply atomically (all-or-nothing): a rejected blocker aborts the
    whole command without persisting. The output distinguishes edges that were
    actually drawn from ones skipped as no-ops (AX review 2026-07-29).
    """
    import trek
    import trek_store

    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    target_id = os.environ.get("BEACON_TREK_TARGET_ID", "").strip()
    blockers_raw = os.environ.get("BEACON_TREK_BLOCKER_IDS", "")
    note = os.environ.get("BEACON_TREK_NOTE", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    blocker_ids = [b.strip() for b in blockers_raw.split("\n") if b.strip()]

    if not trek_id:
        print("Error: trek_id is required", file=sys.stderr)
        sys.exit(1)
    if not target_id:
        print("Error: target_id is required (e.g. ms-42 or e-2034)",
              file=sys.stderr)
        sys.exit(1)
    if not blocker_ids:
        print("Error: at least one --on <blocker-target-id> is required",
              file=sys.stderr)
        sys.exit(1)

    def _report(doc):
        # Honest per-edge report: an edge that made it into the ledger was drawn;
        # one that didn't (and the command didn't error) was a satisfied no-op.
        recorded = set((doc.get("target_blockers") or {}).get(target_id) or [])
        drawn = [b for b in blocker_ids if b in recorded]
        skipped = [b for b in blocker_ids if b not in recorded]
        if json_mode:
            print(json.dumps(doc, ensure_ascii=False))
            return
        for b in drawn:
            print(f"  blocked on {b}")
        for b in skipped:
            print(f"  skipped {b} (= 既に取り込み済み、依存は自動的に満たされています)")
        if drawn:
            print(f"→ trek {trek_id} target {target_id} は依存待ち (block) です。"
                  " 依存先が leader_review (= 取り込み済み) に達すると自動再開します。")
        else:
            print(f"→ 何も張られていません (全 blocker が既に満たされています)。"
                  f" target {target_id} の状態は変わっていません。")

    if _is_cloud_mode():
        try:
            client, _config = _get_api_client()
            t = client.add_trek_blocker(
                trek_id, target_id=target_id,
                blocker_target_ids=blocker_ids, note=note,
            )
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        _report(t)
        return

    t = trek_store.load_trek(trek_id)
    if t is None:
        print(f"Error: trek {trek_id} not found", file=sys.stderr)
        sys.exit(1)
    caller_sid = os.environ.get("BEACON_SESSION_ID", "")
    # Apply all to the in-memory doc; save once. A rejection aborts before save
    # → atomic, matching the cloud endpoint (AX/maintainability review).
    for b in blocker_ids:
        try:
            trek.add_blocker(
                t, target_id=target_id, blocker_target_id=b,
                updated_by_session_id=caller_sid, note=note,
            )
        except trek.TrekBlockerError as e:
            print(f"Error ({e.kind}): {e}", file=sys.stderr)
            sys.exit(1)
    trek_store.save_trek(t)
    _report(t)


def cmd_trek_unblock():
    """Remove a blocker edge and reconcile (ms-128 方針4 / e-4365).

    Env:
      BEACON_TREK_ID          (required)
      BEACON_TREK_TARGET_ID   (required, the dependent target)
      BEACON_TREK_BLOCKER_IDS (required, the blocker edge to drop)
      BEACON_JSON             "1" → json output

    The leader's cycle-breaking escape hatch: drops the TARGET → BLOCKER edge and
    reconciles so the target's block state re-settles (unblocks if that was its
    last unsatisfied blocker). Leader-only in cloud mode.
    """
    import trek
    import trek_store

    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    target_id = os.environ.get("BEACON_TREK_TARGET_ID", "").strip()
    blockers_raw = os.environ.get("BEACON_TREK_BLOCKER_IDS", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    blocker_ids = [b.strip() for b in blockers_raw.split("\n") if b.strip()]

    if not trek_id or not target_id or not blocker_ids:
        print("Error: trek_id, target_id and --on <blocker-id> are required",
              file=sys.stderr)
        sys.exit(1)
    blocker_id = blocker_ids[0]

    if _is_cloud_mode():
        try:
            client, _config = _get_api_client()
            t = client.remove_trek_blocker(
                trek_id, target_id=target_id, blocker_target_id=blocker_id,
            )
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        t = trek_store.load_trek(trek_id)
        if t is None:
            print(f"Error: trek {trek_id} not found", file=sys.stderr)
            sys.exit(1)
        removed = trek.remove_blocker(
            t, target_id=target_id, blocker_target_id=blocker_id,
        )
        if removed:
            trek.reconcile_blocks(t, updated_by_session_id="server")
            trek_store.save_trek(t)

    if json_mode:
        print(json.dumps(t, ensure_ascii=False))
    else:
        state = "?"
        try:
            state = trek.get_task_state(t, target_id)
        except Exception:
            pass
        print(f"Removed blocker {target_id} → {blocker_id}. "
              f"target {target_id} state: {state}")


def cmd_trek_blockers():
    """Show the blocked targets of a Trek and what each is waiting on (e-4365).

    Env:
      BEACON_TREK_ID   (required)
      BEACON_JSON      "1" → json output (the blocked_queue list)

    Reads the trek's dependency ledger + task states and prints, per blocked
    target, its blockers and the ones still unsatisfied. In local mode this also
    runs a reconcile first (local has no server tick driver, so auto-unblock
    would otherwise never fire — AX review 2026-07-29). Cloud mode reads the
    server-reconciled state as-is.
    """
    import trek
    import trek_store
    import trek_scheduler

    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not trek_id:
        print("Error: trek_id is required", file=sys.stderr)
        sys.exit(1)

    if _is_cloud_mode():
        try:
            client, _config = _get_api_client()
            t = client.get_trek(trek_id)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        t = trek_store.load_trek(trek_id)
        if t is not None:
            # Local mode has no server tick — reconcile lazily so `blockers`
            # reflects auto-unblock / rollback before we render it.
            result = trek.reconcile_blocks(t, updated_by_session_id="server")
            if result.get("unblocked") or result.get("reblocked"):
                trek_store.save_trek(t)
    if t is None:
        print(f"Error: trek {trek_id} not found", file=sys.stderr)
        sys.exit(1)

    agg = trek_scheduler.build_task_state_aggregate(t)
    blocked = agg.get("blocked_queue") or []
    cycles = []
    try:
        import trek as _trek
        cycles = _trek.detect_blocker_cycles(t)
    except Exception:
        cycles = []

    if json_mode:
        print(json.dumps(
            {"blocked_queue": blocked, "blocker_cycles": cycles},
            ensure_ascii=False,
        ))
        return

    if not blocked and not cycles:
        print(f"trek {trek_id}: 依存待ち (block) の target はありません。")
        return
    if blocked:
        print(f"trek {trek_id}: {len(blocked)} 件が依存待ち (block):")
        for row in blocked:
            unsat = ", ".join(row.get("unsatisfied") or []) or "(none)"
            allb = ", ".join(row.get("blockers") or [])
            print(f"  - {row['task_id']}: 待ち {unsat}  (全依存: {allb})")
    if cycles:
        print(f"⚠ 依存の循環 {len(cycles)} 件:")
        for c in cycles:
            print(f"  - {' → '.join(c)} → {c[0]}")
        print(
            "  まずリーダーが自律解消を試みてください: `beacon trek unblock "
            "<trek> <target> --on <blocker>` で循環上の依存を 1 本外す "
            "(= target 分割 / 順序強制 も可)。解けない時のみユーザーへ escalate。"
        )


def cmd_trek_extend_ttl():
    """Postpone TTL safety net deadline on a Trek task (ms-95 / e-2308).

    Leader-side primitive for the Agent-tool subagent dispatch path:
    main session calls this before launching a subagent that cannot
    stamp ``last_activity_at`` itself, so the auto-stall deadline does
    not fire mid-delegation. ``--minutes 0`` clears the extension.

    Env:
      BEACON_TREK_ID            (required)
      BEACON_TREK_TASK_ID       (required)
      BEACON_TREK_TTL_MINUTES   (required, int; ≤0 clears)
      BEACON_TREK_TTL_REASON    (optional, short audit string)
      BEACON_JSON               "1" → json output
    """
    import trek
    import trek_store

    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    task_id = os.environ.get("BEACON_TREK_TASK_ID", "").strip()
    minutes_raw = os.environ.get("BEACON_TREK_TTL_MINUTES", "").strip()
    reason = os.environ.get("BEACON_TREK_TTL_REASON", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not trek_id:
        print("Error: trek_id is required", file=sys.stderr)
        sys.exit(1)
    if not task_id:
        print("Error: task_id is required (e.g. e-2034)", file=sys.stderr)
        sys.exit(1)
    if not minutes_raw:
        print(
            "Error: --minutes is required (use 0 to clear an existing "
            "extension)",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        minutes = int(minutes_raw)
    except ValueError:
        print(
            f"Error: --minutes must be an integer (got {minutes_raw!r})",
            file=sys.stderr,
        )
        sys.exit(1)

    if _is_cloud_mode():
        try:
            client, _config = _get_api_client()
            entry = client.extend_trek_task_ttl(
                trek_id,
                task_id=task_id,
                minutes=minutes,
                reason=reason,
            )
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        if json_mode:
            print(json.dumps(entry, ensure_ascii=False))
        else:
            ext = (entry or {}).get("ttl_extended_until") or ""
            if ext:
                print(
                    f"Extended trek {trek_id} task {task_id} TTL until {ext} "
                    f"(+{minutes} min)"
                )
            else:
                print(
                    f"Cleared TTL extension on trek {trek_id} task {task_id} "
                    f"(normal TTL resumes on next scheduler tick)"
                )
        return

    t = trek_store.load_trek(trek_id)
    if t is None:
        print(f"Error: trek {trek_id} not found", file=sys.stderr)
        sys.exit(1)
    try:
        trek.extend_task_ttl(
            t,
            task_id=task_id,
            minutes=minutes,
            reason=reason,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    trek_store.save_trek(t)
    entry = (t.get("task_states") or {}).get(task_id) or {}
    if json_mode:
        print(json.dumps(entry, ensure_ascii=False))
    else:
        ext = entry.get("ttl_extended_until") or ""
        if ext:
            print(
                f"Extended trek {trek_id} task {task_id} TTL until {ext} "
                f"(+{minutes} min, local mode)"
            )
        else:
            print(
                f"Cleared TTL extension on trek {trek_id} task {task_id} "
                "(local mode)"
            )


def cmd_trek_summary_sent():
    """Stamp ``meta.summary_sent_at`` after the leader sent the user
    summary DM (ms-97 / Phase 7-A / AC21).

    Leader-only on the server side (= ``_require_trek_leader_session``
    hard-check on phase A+ trek)。 stamp 後は ``completion_notified_at``
    と組み合わせて leader-digest tick が停止する。 Cloud-mode only
    (= server endpoint 経由)。 ローカル mode では scheduler tick が
    存在しないため stamp する意味がない。

    Env:
      BEACON_TREK_ID  (required)
      BEACON_JSON     "1" → json output
    """
    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not trek_id:
        print("Error: trek_id is required", file=sys.stderr)
        sys.exit(1)

    if not _is_cloud_mode():
        print(
            "Error: beacon trek summary-sent is cloud-mode only "
            "(= server endpoint stamps meta.summary_sent_at; local treks "
            "have no scheduler tick to stop)",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        client, _config = _get_api_client()
        t = client.trek_summary_sent(trek_id)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if json_mode:
        print(json.dumps(t, ensure_ascii=False))
    else:
        stamped = (t.get("meta") or {}).get("summary_sent_at") or "(unknown)"
        print(
            f"Stamped meta.summary_sent_at={stamped} on trek {trek_id}. "
            "Leader-digest tick will stop once completion_ready has also "
            "fired."
        )


def cmd_trek_plan():
    """Edit a trek's scope (= what work items the trek is concerned with).

    Env (exactly one of add/remove must be set):
      BEACON_TREK_ID            (required)
      BEACON_TREK_SCOPE_ADD     "<project>[:<ref>]" — append a scope entry
      BEACON_TREK_SCOPE_REMOVE  "<project>[:<ref>]" — remove a scope entry
      BEACON_JSON               "1" → json output

    ``ref`` prefix dispatches: ``ms-...`` → milestone, ``op-...`` → operation,
    ``e-...`` → task; omitted = project-wide scope.
    """
    import trek
    import trek_store

    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    add_arg = os.environ.get("BEACON_TREK_SCOPE_ADD", "").strip()
    remove_arg = os.environ.get("BEACON_TREK_SCOPE_REMOVE", "").strip()
    # ms-75 / e-1865: optional goal_state setter. Empty string clears it.
    # ``BEACON_TREK_GOAL_STATE_SET`` distinguishes "user passed --goal-state ''"
    # (= clear) from "user did not pass --goal-state at all" (= preserve).
    goal_state_arg = os.environ.get("BEACON_TREK_GOAL_STATE", "")
    goal_state_explicit = (
        os.environ.get("BEACON_TREK_GOAL_STATE_SET", "") == "1"
    )
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not trek_id:
        print("Error: trek_id is required", file=sys.stderr)
        sys.exit(1)
    if not add_arg and not remove_arg and not goal_state_explicit:
        print(
            "Error: --add-scope <ref>, --remove-scope <ref>, or --goal-state "
            "<text> is required",
            file=sys.stderr,
        )
        sys.exit(1)
    if add_arg and remove_arg:
        print(
            "Error: pass --add-scope or --remove-scope, not both in one call",
            file=sys.stderr,
        )
        sys.exit(1)

    # Parse the scope arg with the shared helper so cloud and local agree on
    # the entry shape. We do this BEFORE branching so syntax errors surface
    # the same way regardless of mode. goal_state-only calls skip parsing.
    entry: dict | None = None
    if add_arg or remove_arg:
        try:
            entry = trek.parse_scope_arg(add_arg or remove_arg)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    # ``pending_rec`` is the local-mode handle to the freshly-staged pending
    # op record (ms-97 / e-2626 + e-2611). Initialise to None so the print
    # block downstream can safely query ``(pending_rec or {}).get(...)``
    # even on goal_state-only invocations that never enter the staging path.
    pending_rec: dict | None = None

    if _is_cloud_mode():
        try:
            client, _config = _get_api_client()
            t = None
            if entry is not None:
                kwargs = {
                    "project": entry["project"],
                    "milestone": entry.get("milestone", ""),
                    "operation": entry.get("operation", ""),
                    "task": entry.get("task", ""),
                }
                if add_arg:
                    t = client.add_trek_scope(trek_id, **kwargs)
                else:
                    t = client.remove_trek_scope(trek_id, **kwargs)
            if goal_state_explicit:
                # ms-75 / e-1865: goal_state setter via cloud API. The server
                # endpoint is wired in a follow-up (cloud-mode parity will
                # land alongside the rest of e-1865 server work); in the
                # meantime cloud users get a graceful warning rather than a
                # silent no-op.
                if hasattr(client, "set_trek_goal_state"):
                    t = client.set_trek_goal_state(
                        trek_id, goal_state=goal_state_arg,
                    )
                else:
                    print(
                        "warn: --goal-state is local-mode only until the "
                        "server endpoint lands (e-1865 cloud parity). "
                        "Set it locally first; the cloud copy will be "
                        "updated by the next sync.",
                        file=sys.stderr,
                    )
                    if t is None:
                        # Nothing to print downstream — bail without error so
                        # the user sees the warning and can re-plan.
                        sys.exit(0)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        t = trek_store.load_trek(trek_id)
        if t is None:
            print(f"Error: trek {trek_id} not found", file=sys.stderr)
            sys.exit(1)
        try:
            if entry is not None:
                if add_arg:
                    # ms-97 / e-2626 (AC23) — scope-add now stages a pending op
                    # (= ``pending_user_approval`` state). User commits with
                    # ``beacon trek scope-approve <pending_id>``. Mirrors the
                    # scope-remove flip in e-2611. AC24 blanket pre-approval
                    # lands as a layer on top in a follow-up task (= e-2603).
                    pending_rec = trek.add_pending_scope_op(
                        t,
                        action=trek.PENDING_SCOPE_ACTION_ADD,
                        entry=entry,
                        requested_by_session_id=_resolve_local_session_id(),
                    )
                else:
                    # ms-97 / e-2611 — scope-remove also stages a pending op.
                    # User commits with ``beacon trek scope-approve <id>``.
                    pending_rec = trek.add_pending_scope_op(
                        t,
                        action=trek.PENDING_SCOPE_ACTION_REMOVE,
                        entry=entry,
                        requested_by_session_id=_resolve_local_session_id(),
                    )
            if goal_state_explicit:
                trek.set_goal_state(t, goal_state=goal_state_arg)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        trek_store.save_trek(t)

    if json_mode:
        print(json.dumps(t, ensure_ascii=False))
    else:
        if add_arg:
            ref_display = add_arg
            # ms-97 / e-2626 (AC23) — scope-add now stages a pending op.
            # Show the pending_id so the user knows the exact handle for
            # the approve / reject CLI. Cloud-mode path returns a
            # ``pending_op`` field on the response payload (= server add
            # endpoint) so json-mode consumers get the same info.
            pending_id = ""
            if _is_cloud_mode():
                po = (t or {}).get("pending_op") or {}
                pending_id = po.get("pending_id") or ""
            else:
                pending_id = (pending_rec or {}).get("pending_id") or ""
            print(
                f"Staged scope-add of {ref_display} on trek {trek_id} "
                f"(pending_id: {pending_id}). "
                f"Approve: beacon trek scope-approve {pending_id} "
                f"(reject: beacon trek scope-reject {pending_id})"
            )
        elif remove_arg:
            ref_display = remove_arg
            # Show the pending_id so the user knows the exact handle
            # for the approve / reject CLI. Cloud-mode path also returns
            # a ``pending_op`` field on the response payload (see server
            # remove endpoint) so json-mode consumers get the same info.
            pending_id = ""
            if _is_cloud_mode():
                po = (t or {}).get("pending_op") or {}
                pending_id = po.get("pending_id") or ""
            else:
                pending_id = (pending_rec or {}).get("pending_id") or ""
            print(
                f"Staged scope-remove of {ref_display} on trek {trek_id} "
                f"(pending_id: {pending_id}). "
                f"Approve: beacon trek scope-approve {pending_id} "
                f"(reject: beacon trek scope-reject {pending_id})"
            )
        if goal_state_explicit:
            new_val = (goal_state_arg or "").strip()
            if new_val:
                print(f"goal_state set on trek {trek_id}: \"{new_val}\"")
            else:
                print(f"goal_state cleared on trek {trek_id}")


def _resolve_local_session_id() -> str:
    """Best-effort resolution of the current bclaude session id.

    Used for stamping ``requested_by_session_id`` on pending scope ops in
    local mode (= the cloud-mode path resolves the session from the
    ``X-Beacon-Session`` request header on the server side). Falls back
    to an empty string when no session is bound — the pending record
    still works; only the attribution is blank.
    """
    sid = os.environ.get("BEACON_SESSION_ID", "").strip()
    if sid:
        return sid
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    return sid


def _cloud_slot_client():
    """Return (client, config) for cloud-mode slot operations, or None.

    ms-99 / e-2830: shared bootstrap for the four slot verbs. Falls
    back to None so callers can degrade gracefully rather than crash
    (matches the pre-e-2830 stub warning contract).
    """
    try:
        return _get_api_client()
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_trek_slot_add():
    """Stage a slot-add pending op with a fresh ``sl-<8 hex>`` id.

    Env:
      BEACON_TREK_ID          (required)
      BEACON_SLOT_PROJECT     (required — project_id the slot lives in)
      BEACON_SLOT_MILESTONE   narrowing key (one of the two target-entities)
      BEACON_SLOT_OPERATION
      BEACON_SLOT_TASK        DEPRECATED (ms-128 方針3): a Trek Target is a
                              target-entity, not a single task. Passing this is
                              rejected with guidance to use --milestone (+
                              --children to narrow to specific tasks).
      BEACON_SLOT_CHILDREN    optional comma-separated e-ids for MS slots
      BEACON_JSON             "1" → json output
    """
    import trek
    import trek_store

    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    project = os.environ.get("BEACON_SLOT_PROJECT", "").strip()
    milestone = os.environ.get("BEACON_SLOT_MILESTONE", "").strip()
    operation = os.environ.get("BEACON_SLOT_OPERATION", "").strip()
    task = os.environ.get("BEACON_SLOT_TASK", "").strip()
    children_raw = os.environ.get("BEACON_SLOT_CHILDREN", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not trek_id:
        print("Error: trek_id is required", file=sys.stderr)
        sys.exit(1)
    if not project:
        print("Error: --project <pid> is required", file=sys.stderr)
        sys.exit(1)
    if task:
        # ms-128 方針3 (v2.1): Trek の Target は target-entity (target-class
        # インスタンス = milestone / operation …) に限る。単一タスクは固有の
        # レビュー lifecycle (leader_review / user_review は target 粒度) を
        # 持たないので Target にできない。特定タスクに絞りたい場合は親
        # milestone を --milestone で指定し、--children e-XXX で絞る (= 既存の
        # included_task_ids 機構、機能損失なし)。
        print(
            "Error: Trek の Target は target-entity (milestone / operation) "
            "です。単一タスクは scope できません。親 milestone を --milestone "
            "で指定し、特定タスクに絞るなら --children e-XXX を併用してください "
            "(ms-128 方針3)。",
            file=sys.stderr,
        )
        sys.exit(1)
    narrowing_count = sum(1 for v in (milestone, operation) if v)
    if narrowing_count == 0:
        print(
            "Error: one of --milestone | --operation is required "
            "(= slot must narrow the project to a target-entity, "
            "ms-97 AC7 / ms-128 方針3)",
            file=sys.stderr,
        )
        sys.exit(1)
    if narrowing_count > 1:
        print(
            "Error: pass exactly one of --milestone / --operation",
            file=sys.stderr,
        )
        sys.exit(1)

    children_list = [c.strip() for c in children_raw.split(",") if c.strip()]
    entry: dict = {"project": project}
    if milestone:
        entry["milestone"] = milestone
    elif operation:
        entry["operation"] = operation
    elif task:
        entry["task"] = task
    # ``--children`` only makes sense for MS slots (SPEC 方針 2). For
    # task/op slots the child list is meaningless; refuse politely.
    if children_list and not milestone:
        print(
            "Error: --children is only valid with --milestone (MS slot "
            "child opt-in, SPEC 方針 2)",
            file=sys.stderr,
        )
        sys.exit(1)
    if children_list:
        entry["included_task_ids"] = children_list

    if _is_cloud_mode():
        client, _config = _cloud_slot_client()
        try:
            resp = client.add_trek_slot(
                trek_id,
                project=project,
                milestone=milestone,
                operation=operation,
                task=task,
                included_task_ids=(children_list if milestone else None),
            )
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        pending = resp.get("pending_op") or {}
        slot_id = (pending.get("entry") or {}).get("slot_id") or ""
        pending_id = pending.get("pending_id") or ""
        if json_mode:
            print(json.dumps({
                "pending_id": pending_id,
                "slot_id": slot_id,
                "entry": pending.get("entry") or {},
            }, ensure_ascii=False))
        else:
            target_ref = milestone or operation or task
            print(
                f"Staged scope-add on trek {trek_id}: "
                f"target={project}:{target_ref} (id={slot_id}) "
                f"(pending_id: {pending_id}). "
                f"Approve: beacon trek scope-approve {trek_id} {pending_id}"
            )
        return

    t = trek_store.load_trek(trek_id)
    if t is None:
        print(f"Error: trek {trek_id} not found", file=sys.stderr)
        sys.exit(1)
    try:
        rec = trek.add_pending_scope_op(
            t,
            action=trek.PENDING_SCOPE_ACTION_ADD,
            entry=entry,
            requested_by_session_id=_resolve_local_session_id(),
            mint_slot=True,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    trek_store.save_trek(t)

    slot_id = (rec.get("entry") or {}).get("slot_id") or ""
    pending_id = rec.get("pending_id") or ""
    if json_mode:
        print(json.dumps({
            "pending_id": pending_id,
            "slot_id": slot_id,
            "entry": rec.get("entry") or {},
        }, ensure_ascii=False))
    else:
        target_ref = milestone or operation or task
        print(
            f"Staged scope-add on trek {trek_id}: "
            f"target={project}:{target_ref} (id={slot_id}) "
            f"(pending_id: {pending_id}). "
            f"Approve: beacon trek scope-approve {trek_id} {pending_id}"
        )


def cmd_trek_slot_amend():
    """Stage a slot-amend pending op (= edit ``included_task_ids``).

    Env:
      BEACON_TREK_ID              (required)
      BEACON_SLOT_ID              (required)
      BEACON_SLOT_ADD_CHILDREN    comma-separated e-ids to add
      BEACON_SLOT_REMOVE_CHILDREN comma-separated e-ids to remove
      BEACON_JSON                 "1" → json output
    """
    import trek
    import trek_store

    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    slot_id = os.environ.get("BEACON_SLOT_ID", "").strip()
    add_raw = os.environ.get("BEACON_SLOT_ADD_CHILDREN", "").strip()
    rem_raw = os.environ.get("BEACON_SLOT_REMOVE_CHILDREN", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not trek_id or not slot_id:
        print("Error: trek_id and target id are required", file=sys.stderr)
        sys.exit(1)
    add_list = [c.strip() for c in add_raw.split(",") if c.strip()]
    rem_list = [c.strip() for c in rem_raw.split(",") if c.strip()]
    if not add_list and not rem_list:
        print(
            "Error: at least one --add-child or --remove-child is required",
            file=sys.stderr,
        )
        sys.exit(1)

    if _is_cloud_mode():
        client, _config = _cloud_slot_client()
        try:
            resp = client.amend_trek_slot(
                trek_id, slot_id,
                add_children=add_list,
                remove_children=rem_list,
            )
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        pending = resp.get("pending_op") or {}
        pending_id = pending.get("pending_id") or ""
        if json_mode:
            print(json.dumps(pending, ensure_ascii=False))
        else:
            print(
                f"Staged scope-amend on trek {trek_id}, target id={slot_id} "
                f"(pending_id: {pending_id}). "
                f"add={add_list} remove={rem_list}. "
                f"Approve: beacon trek scope-approve {trek_id} {pending_id}"
            )
        return

    t = trek_store.load_trek(trek_id)
    if t is None:
        print(f"Error: trek {trek_id} not found", file=sys.stderr)
        sys.exit(1)
    try:
        rec = trek.add_pending_slot_amend_op(
            t,
            slot_id=slot_id,
            add_children=add_list,
            remove_children=rem_list,
            requested_by_session_id=_resolve_local_session_id(),
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    trek_store.save_trek(t)

    pending_id = rec.get("pending_id") or ""
    if json_mode:
        print(json.dumps(rec, ensure_ascii=False))
    else:
        print(
            f"Staged scope-amend on trek {trek_id}, target id={slot_id} "
            f"(pending_id: {pending_id}). "
            f"add={add_list} remove={rem_list}. "
            f"Approve: beacon trek scope-approve {trek_id} {pending_id}"
        )


def cmd_trek_slot_claim():
    """Stage a slot-claim pending op (= stamp claim_session_id + claimed_at).

    Env:
      BEACON_TREK_ID       (required)
      BEACON_SLOT_ID       (required)
      BEACON_SLOT_SESSION  session_id override (default: current session)
      BEACON_SLOT_UNCLAIM  "1" → clear the claim (empty session_id)
      BEACON_JSON          "1" → json output
    """
    import trek
    import trek_store

    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    slot_id = os.environ.get("BEACON_SLOT_ID", "").strip()
    session_override = os.environ.get("BEACON_SLOT_SESSION", "").strip()
    unclaim = os.environ.get("BEACON_SLOT_UNCLAIM", "") == "1"
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not trek_id or not slot_id:
        print("Error: trek_id and target id are required", file=sys.stderr)
        sys.exit(1)
    if unclaim:
        session_id = ""
    else:
        session_id = session_override or _resolve_local_session_id()
        if not session_id:
            print(
                "Error: no session_id resolved. Pass --session <sid> or "
                "run inside a bclaude session (= BEACON_SESSION_ID set)",
                file=sys.stderr,
            )
            sys.exit(1)

    if _is_cloud_mode():
        client, _config = _cloud_slot_client()
        try:
            resp = client.claim_trek_slot(
                trek_id, slot_id, session_id=session_id,
            )
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        pending = resp.get("pending_op") or {}
        pending_id = pending.get("pending_id") or ""
        if json_mode:
            print(json.dumps(pending, ensure_ascii=False))
        else:
            verb = "unclaim" if not session_id else f"claim by {session_id}"
            print(
                f"Staged scope-{verb} on trek {trek_id}, target id={slot_id} "
                f"(pending_id: {pending_id}). "
                f"Approve: beacon trek scope-approve {trek_id} {pending_id}"
            )
        return

    t = trek_store.load_trek(trek_id)
    if t is None:
        print(f"Error: trek {trek_id} not found", file=sys.stderr)
        sys.exit(1)
    try:
        rec = trek.add_pending_slot_claim_op(
            t,
            slot_id=slot_id,
            session_id=session_id,
            requested_by_session_id=_resolve_local_session_id(),
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    trek_store.save_trek(t)

    pending_id = rec.get("pending_id") or ""
    if json_mode:
        print(json.dumps(rec, ensure_ascii=False))
    else:
        verb = "unclaim" if not session_id else f"claim by {session_id}"
        print(
            f"Staged scope-{verb} on trek {trek_id}, target id={slot_id} "
            f"(pending_id: {pending_id}). "
            f"Approve: beacon trek scope-approve {trek_id} {pending_id}"
        )


def cmd_trek_slot_list():
    """List slot rows (materialized shape) for ``beacon trek slot list``.

    Phase 1 view: projects each on-disk scope entry through
    ``trek.materialize_slot_view`` — v2 attributes + narrowing keys +
    child opt-in list + claim. Phase 2 (e-2832) adds full expansion of
    the child set via ``materialize_slots``.
    """
    import trek
    import trek_store

    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not trek_id:
        print("Error: trek_id is required", file=sys.stderr)
        sys.exit(1)

    if _is_cloud_mode():
        client, _config = _cloud_slot_client()
        try:
            resp = client.list_trek_slots(trek_id)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        rows = resp.get("slots") or []
        if json_mode:
            print(json.dumps(rows, ensure_ascii=False))
            return
        if not rows:
            print(f"(no targets on trek {trek_id})")
            return
        for r in rows:
            children = r.get("included_task_ids")
            if children is None:
                children_repr = "(legacy: all children)"
            else:
                children_repr = f"{len(children)} explicit: {children}"
            claim = r.get("claim_session_id") or "(unclaimed)"
            print(
                f"- {r.get('target_kind', '')}={r.get('target_id', '')} "
                f"(id={r.get('slot_id') or '(legacy-no-id)'}) "
                f"project={r.get('project', '')} "
                f"children={children_repr} claim={claim}"
            )
        return

    t = trek_store.load_trek(trek_id)
    if t is None:
        print(f"Error: trek {trek_id} not found", file=sys.stderr)
        sys.exit(1)
    rows = trek.materialize_slot_view(t)
    if json_mode:
        print(json.dumps(rows, ensure_ascii=False))
    else:
        if not rows:
            print(f"(no targets on trek {trek_id})")
            return
        for r in rows:
            children = r.get("included_task_ids")
            if children is None:
                children_repr = "(legacy: all children)"
            else:
                children_repr = f"{len(children)} explicit: {children}"
            claim = r.get("claim_session_id") or "(unclaimed)"
            print(
                f"- {r['target_kind']}={r['target_id']} "
                f"(id={r['slot_id'] or '(legacy-no-id)'}) "
                f"project={r['project']} "
                f"children={children_repr} claim={claim}"
            )


def cmd_trek_scope_approve():
    """Commit a pending scope op (= apply add or remove).

    ms-97 / e-2611 AC25 — Mirror of scope-add approval. Reads
    BEACON_TREK_ID + BEACON_PENDING_ID and either calls the server
    approve endpoint (cloud mode) or applies in-place via
    ``trek.approve_pending_scope_op`` (local mode).
    """
    import trek
    import trek_store

    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    pending_id = os.environ.get("BEACON_PENDING_ID", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not trek_id or not pending_id:
        print("Error: trek_id and pending_id are required", file=sys.stderr)
        sys.exit(1)

    if _is_cloud_mode():
        try:
            client, _config = _get_api_client()
            if hasattr(client, "approve_trek_scope_op"):
                t = client.approve_trek_scope_op(trek_id, pending_id)
            else:
                # Fallback: hit the endpoint directly via the client's
                # raw HTTP helper. The api_client method lands in a
                # follow-up; CLI users get parity through the raw POST.
                t = client.post(
                    f"/api/treks/{trek_id}/scope/approve/{pending_id}",
                    body={},
                )
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        t = trek_store.load_trek(trek_id)
        if t is None:
            print(f"Error: trek {trek_id} not found", file=sys.stderr)
            sys.exit(1)
        try:
            trek.approve_pending_scope_op(t, pending_id=pending_id)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        trek_store.save_trek(t)

    if json_mode:
        print(json.dumps(t, ensure_ascii=False))
    else:
        print(
            f"Approved pending scope op {pending_id} on trek {trek_id} "
            f"(scope: {len(t.get('scope') or [])} items, "
            f"pending: {len(t.get('pending_scope_ops') or [])} items)"
        )


def cmd_trek_scope_reject():
    """Drop a pending scope op without applying it.

    ms-97 / e-2611 AC25 — Companion to scope-approve. Same env contract.
    """
    import trek
    import trek_store

    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    pending_id = os.environ.get("BEACON_PENDING_ID", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not trek_id or not pending_id:
        print("Error: trek_id and pending_id are required", file=sys.stderr)
        sys.exit(1)

    if _is_cloud_mode():
        try:
            client, _config = _get_api_client()
            if hasattr(client, "reject_trek_scope_op"):
                t = client.reject_trek_scope_op(trek_id, pending_id)
            else:
                t = client.post(
                    f"/api/treks/{trek_id}/scope/reject/{pending_id}",
                    body={},
                )
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        t = trek_store.load_trek(trek_id)
        if t is None:
            print(f"Error: trek {trek_id} not found", file=sys.stderr)
            sys.exit(1)
        try:
            trek.reject_pending_scope_op(t, pending_id=pending_id)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        trek_store.save_trek(t)

    if json_mode:
        print(json.dumps(t, ensure_ascii=False))
    else:
        print(
            f"Rejected pending scope op {pending_id} on trek {trek_id} "
            f"(pending: {len(t.get('pending_scope_ops') or [])} items)"
        )


def _cmd_trek_blanket(direction: str):
    """Shared implementation for blanket-approve / blanket-revoke.

    ms-97 / Phase 7-C / AC24 — Cloud / local mode both supported. Cloud
    mode hits the new ``/api/treks/{id}/blanket-approve`` (or revoke)
    endpoint; local mode mutates the trek doc in place via the trek
    helpers and saves through ``trek_store``.
    """
    import trek
    import trek_store

    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    category = os.environ.get("BEACON_TREK_BLANKET_CATEGORY", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not trek_id or not category:
        print(
            "Error: trek_id and --category are required",
            file=sys.stderr,
        )
        sys.exit(1)

    path = "blanket-approve" if direction == "approve" else "blanket-revoke"
    if _is_cloud_mode():
        try:
            client, _config = _get_api_client()
            payload = client.post(
                f"/api/treks/{trek_id}/{path}",
                body={"category": category},
            )
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        result = payload
    else:
        t = trek_store.load_trek(trek_id)
        if t is None:
            print(f"Error: trek {trek_id} not found", file=sys.stderr)
            sys.exit(1)
        try:
            if direction == "approve":
                trek.add_blanket_approval(t, category)
            else:
                trek.remove_blanket_approval(t, category)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        trek_store.save_trek(t)
        result = {
            "trek_id": trek_id,
            "blanket_scope_approvals": trek.list_blanket_approvals(t),
        }

    if json_mode:
        print(json.dumps(result, ensure_ascii=False))
    else:
        verb = "Added" if direction == "approve" else "Removed"
        approvals = result.get("blanket_scope_approvals") or []
        print(
            f"{verb} blanket category '{category}' on trek {trek_id} "
            f"(active categories: {approvals})"
        )


def cmd_trek_blanket_approve():
    """Register a blanket scope-add pre-approval (ms-97 / AC24, e-2603)."""
    _cmd_trek_blanket("approve")


def cmd_trek_blanket_revoke():
    """Remove a blanket scope-add pre-approval (ms-97 / AC24, e-2603)."""
    _cmd_trek_blanket("revoke")


def cmd_trek_task_add():
    """Cross-project task add through Trek scope (ms-92 / e-2141).

    The caller specifies a target ``<pid>:<ms-id>`` plus task description.
    The CLI (here) and the server share one scope-guard helper
    (``trek.check_trek_task_add_allowed``) so a single decision drives
    both a 403 reject server-side and a friendly CLI error here.

    Local mode does not (yet) support cross-project task add because the
    local data store doesn't carry a project_id → path registry — the
    feature is genuinely cloud-shaped and the local refuse keeps the
    failure mode explicit instead of pretending to work. Cloud mode
    posts to ``POST /api/treks/{trek_id}/task-add`` which performs the
    same scope walk + writes the task to the target project's MS,
    stamping ``meta.trek_id`` on the entry for audit-trail traceability.

    Env:
      BEACON_TREK_ID                (required, e.g. tk-abcd1234)
      BEACON_TREK_TASK_TARGET       (required, "<project-id>:<ms-id>")
      BEACON_DESCRIPTION            (required, the task description)
      BEACON_PRIORITY               (optional, lowest/low/medium/high/highest; middle=alias)
      BEACON_MOTIVATION             (optional)
      BEACON_ACCEPTANCE_CRITERIA    (optional)
      BEACON_TYPE                   (optional, default "task")
      BEACON_JSON                   "1" → json output
    """
    import trek

    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    target = os.environ.get("BEACON_TREK_TASK_TARGET", "").strip()
    description = os.environ.get("BEACON_DESCRIPTION", "").strip()
    priority = os.environ.get("BEACON_PRIORITY", "").strip()
    motivation = os.environ.get("BEACON_MOTIVATION", "")
    acceptance_criteria = os.environ.get("BEACON_ACCEPTANCE_CRITERIA", "")
    entry_type = os.environ.get("BEACON_TYPE", "task").strip() or "task"
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not trek_id:
        print("Error: trek_id is required (e.g. tk-abcd1234)", file=sys.stderr)
        sys.exit(1)
    if not target:
        print(
            "Error: --target <project-id>:<ms-id> is required",
            file=sys.stderr,
        )
        sys.exit(1)
    if not description:
        print("Error: task description is required", file=sys.stderr)
        sys.exit(1)
    if ":" not in target:
        print(
            f"Error: --target {target!r} must be <project-id>:<ms-id> "
            "(omit the colon → project-wide scope which is not allowed "
            "for task add; pick an MS explicitly)",
            file=sys.stderr,
        )
        sys.exit(1)
    target_project, target_ms = target.split(":", 1)
    target_project = target_project.strip()
    target_ms = target_ms.strip()
    if not target_project:
        print(
            f"Error: --target {target!r} missing project_id before ':'",
            file=sys.stderr,
        )
        sys.exit(1)
    if not target_ms or not target_ms.startswith("ms-"):
        print(
            f"Error: --target {target!r} must end with ms-XX "
            "(operations / single tasks are not valid task-add targets; "
            "see SPEC ms-92 e-2141 AC #4 — MS-grain enforcement)",
            file=sys.stderr,
        )
        sys.exit(1)

    # ----------- Cloud mode (= the actual cross-project use case) -----------
    if _is_cloud_mode():
        try:
            client, _config = _get_api_client()
            if not hasattr(client, "add_trek_task"):
                print(
                    "Error: this CLI is paired with an older cloud server "
                    "that doesn't expose POST /api/treks/{trek_id}/task-add. "
                    "Upgrade the server (= ms-92 e-2141 endpoint) or fall "
                    "back to single-project `beacon task add -m <ms-id>` "
                    "for now.",
                    file=sys.stderr,
                )
                sys.exit(1)
            result = client.add_trek_task(
                trek_id,
                target_project=target_project,
                target_milestone=target_ms,
                description=description,
                entry_type=entry_type,
                priority=priority,
                motivation=motivation,
                acceptance_criteria=acceptance_criteria,
            )
        except RuntimeError as e:
            # Server-side 403 / 400 / 404 surfaces here. The server's
            # rejection ``reason`` is included in the error message so
            # the CLI tail line tells the user *why* (project not in
            # scope vs. task-only narrowing vs. milestone mismatch).
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        if json_mode:
            print(json.dumps(result, ensure_ascii=False))
        else:
            eid = result.get("entry_id", "<unknown>")
            print(
                f"Added cross-project task [{eid}] under "
                f"{target_project}:{target_ms} via trek {trek_id}: "
                f"{description}"
            )
        return

    # ----------- Local mode (= rejected with an honest message) -----------
    # We could in principle traverse a registry of local projects, but the
    # local data model doesn't carry one (= local mode is single-project by
    # design). Pretending to succeed by writing into the current cwd's
    # project.json would silently strip the cross-project semantics.
    # The local-mode scope guard *can* still surface the decision so users
    # see the same error vocabulary they'd see from the server.
    import trek_store
    t = trek_store.load_trek(trek_id)
    if t is None:
        print(
            f"Error: trek {trek_id} not found in local store. Cross-project "
            "task add through Trek requires cloud mode (= server endpoint "
            "POST /api/treks/{trek_id}/task-add walks the scope and writes "
            "into the target project). Switch to cloud mode (`beacon cloud "
            "setup`) or add the task directly with `beacon task add -m "
            f"{target_ms}` from inside the target project's cwd.",
            file=sys.stderr,
        )
        sys.exit(1)
    allowed, reason = trek.check_trek_task_add_allowed(
        t, target_project=target_project, target_milestone=target_ms,
    )
    if not allowed:
        print(
            f"Error: trek scope rejects this task add ({reason}). "
            f"trek {trek_id} scope: {t.get('scope') or []}",
            file=sys.stderr,
        )
        sys.exit(2)  # 2 → "scope reject" so callers can distinguish
    print(
        "Error: scope check passed but local mode cannot write across "
        "projects — the local data store is single-project by design. "
        "Switch to cloud mode for cross-project task add, or use "
        f"`beacon task add -m {target_ms}` from inside the target "
        "project's cwd.",
        file=sys.stderr,
    )
    sys.exit(1)


def cmd_trek_leave():
    """Leave a trek (= remove self from members[]).

    Env:
      BEACON_TREK_ID    (required)
      BEACON_USER_EMAIL (required, matched against the member's email)
      BEACON_USER_ID    fallback
      BEACON_JSON       "1" → json output
    """
    import trek
    import trek_store

    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not trek_id:
        print("Error: trek_id is required", file=sys.stderr)
        sys.exit(1)

    user_id, email, _ = _resolve_creator_identity()
    if not email:
        print(
            "Error: BEACON_USER_EMAIL is required to leave a trek",
            file=sys.stderr,
        )
        sys.exit(1)

    if _is_cloud_mode():
        # Cloud path: server identifies the leaver from auth token; rejects
        # leader-removal / last-member with 400.
        try:
            client, _config = _get_api_client()
            t = client.leave_trek(trek_id)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        if json_mode:
            print(json.dumps(t, ensure_ascii=False))
        else:
            print(f"Left trek {trek_id} ({email})")
        return

    t = trek_store.load_trek(trek_id)
    if t is None:
        print(f"Error: trek {trek_id} not found", file=sys.stderr)
        sys.exit(1)

    # ms-97 / e-2658 Phase 1 (AC6) — phase A+ trek の時は session_id grain
    # で自セッションのみ leave (= 同 user の他 session は残す)。 pre-A
    # trek または session_id 不在時は user-grain leave (= 同 user の全
    # entries 削除、 従来挙動)。
    session_id = os.environ.get("BEACON_SESSION_ID", "").strip()
    session_grain = bool(session_id) and trek.is_session_id_keyed(t)
    member = None
    if session_grain:
        member = trek.find_member(t, session_id=session_id)
    if member is None:
        member = trek.find_member_by_email(t, email)
    if member is None:
        member = trek.find_member(t, user_id=user_id)
    if member is None:
        print(
            f"Error: {email} (user_id={user_id}) is not a member of trek "
            f"{trek_id}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        if session_grain and member.get("session_id"):
            trek.remove_member(t, session_id=member["session_id"])
        else:
            trek.remove_member(t, user_id=member["user_id"])
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    trek_store.save_trek(t)

    if json_mode:
        print(json.dumps(t, ensure_ascii=False))
    else:
        print(f"Left trek {trek_id} ({email})")
