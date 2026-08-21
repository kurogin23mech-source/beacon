#!/usr/bin/env python3
"""cmd_milestone.py — the `beacon milestone *` command family (ms-127 e-4849).

Extracted verbatim from commands.py (god-module split). The milestone family is
the largest single command family: add / list / start / done / observe / wait /
release / occupations / join / show / update / delete / purge / depends /
workspace / workspace-cleanup / graph, plus family-private helpers (branch
guards, completion gating, spec-needed trigger, worktree cleanup prompt).

Depends only on commands_shared (upward) + leaf domain modules (core /
work_model / occupation / store / transition_approval), never on commands.py —
acyclic (SPEC 方針4). Four helpers used by BOTH this family and the target /
transition / backlog handlers (_release_occupation_for_transition /
_print_evidence_guidance / _spec_exists_for_ms / _spec_updated_at_for_target)
were promoted into commands_shared as part of this split; both sides import them
from there.

commands.py re-imports the PUBLIC handlers for dispatch + `commands.cmd_milestone_*`;
the family-private helpers (_ensure_on_branch / _milestone_status / ...) and the
family constants (_NotAGitRepoError / _DONE_BAN_ERROR / _OBSERVE_BAN_ERROR) are
NOT re-exported (patch them at cmd_milestone.<name>).

Test patch target (the e-4320 rule): a test driving a cmd_milestone_* handler
must patch EVERY name the handler resolves in cmd_milestone's own namespace —
each `from commands_shared import name` binds an independent copy, so
`monkeypatch.setattr(commands, "get_store", ...)` is a silent no-op on this call
path. Patch `cmd_milestone.<name>` for the handler path. Note the 4 promoted
helpers (_release_occupation_for_transition / _print_evidence_guidance /
_spec_exists_for_ms / _spec_updated_at_for_target) exist as INDEPENDENT bindings
at both `cmd_milestone` and `commands` (both import from commands_shared); patch
`cmd_milestone.<name>` to affect only this family. Do NOT patch
`commands_shared.<name>` to reach a handler — that mutates the shared source and
silently couples every family that imported it (cross-test blast radius).
"""

import os
import sys
import json
import subprocess

import core  # noqa: F401
import work_model  # noqa: F401
import occupation  # noqa: F401
import transition_approval as _ta  # noqa: F401

from commands_shared import (  # noqa: F401
    Optional,
    _HUMAN_UNTRIAGED_REFUSED_MSG,
    _actor_str,
    _ai_session_direct_completion_ban_active,
    _append_changelog,
    _fire_review_due_trigger,
    _gate_target_class,
    _get_triggers_dir,
    _human_untriaged_bypass_refused,
    _local_date,
    _print_evidence_guidance,
    _release_occupation_for_transition,
    _require_reason_or_skip,
    _resolve_current_author,
    _resolve_session_id,
    _spec_exists_for_ms,
    _spec_updated_at_for_target,
    get_store,
    load_project,
    save_project,
)


def cmd_milestone_add():
    title = os.environ.get("BEACON_TITLE", "")
    target_date = os.environ.get("BEACON_TARGET_DATE", "")
    description = os.environ.get("BEACON_DESCRIPTION", "")
    priority = os.environ.get("BEACON_PRIORITY", "")
    objective = os.environ.get("BEACON_OBJECTIVE", "")
    acceptance_criteria = os.environ.get("BEACON_ACCEPTANCE_CRITERIA", "")
    owner = os.environ.get("BEACON_OWNER", "")
    assignee = os.environ.get("BEACON_ASSIGNEE", "")
    # ms-126: priority is now mandatory on the human path. Machine callers
    # (issue import / review-apply / roadmap bulk / dispatch) opt into the
    # ``untriaged`` sentinel by passing ``--untriaged`` (BEACON_ALLOW_UNTRIAGED=1)
    # when they cannot supply a human-judged severity. The bare human command
    # leaves it unset and an empty priority is rejected with the 5-value list.
    allow_untriaged = os.environ.get("BEACON_ALLOW_UNTRIAGED", "") == "1"
    # ms-126 / e-4222: untriaged is a machine-only sentinel. A human session
    # (BEACON_SESSION_KIND=human) that passes --untriaged is refused here — the
    # human owns the priority judgement and must pick one of the 5. Machine / AI
    # sessions (default) keep the untriaged capability. This closes the hole
    # where a person could bypass the mandatory-priority forcing function.
    if _human_untriaged_bypass_refused():
        print(f"Error: {_HUMAN_UNTRIAGED_REFUSED_MSG}", file=sys.stderr)
        sys.exit(1)
    data = load_project()
    _gate_target_class(data, "milestone")  # ms-115: block in non-dev projects
    # ms-43 / e-2281 — stamp the human author on the milestone so the Web
    # UI surfaces the creator label (= 起票者) instead of the legacy
    # ``"claude"`` literal in ``created_by``. Resolution falls back to
    # env > credentials.json > project members[]; unauthenticated local
    # mode returns ``{}`` and the create proceeds without ``meta.author``.
    author = _resolve_current_author(data)
    try:
        ms_id = core.milestone_add(data, title, target_date, description=description,
                                   priority=priority, objective=objective,
                                   acceptance_criteria=acceptance_criteria,
                                   owner=owner, assignee=assignee,
                                   author=author or None,
                                   allow_untriaged=allow_untriaged)
    except ValueError as e:
        # ms-126: surface the "priority is required" forcing function (and the
        # existing "Invalid priority") as a clean CLI error, not a traceback.
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data)
    print(f"Added milestone {ms_id}: {title}")
    if owner or assignee:
        if owner:
            print(f"  owner: {owner}")
        if assignee:
            print(f"  assignee: {assignee}")

    # Promote SPEC creation: fire a trigger so session-start / next prompt
    # surfaces a warning. SPEC = requirements + decision trail (see CORE
    # doc `doc-classification`). This is a soft promotion (warning only),
    # never a hard block — see SPEC for ms-41.
    try:
        _fire_spec_needed_trigger(ms_id, title)
    except Exception:
        pass  # Trigger is best-effort; never block milestone creation.

    # Print an inline hint, but suppress it when the user is doing a bulk
    # add (e.g. /beacon-roadmap registering 5+ MSs in a row). Without this
    # suppression the hint repeats N times and adds noise (e-702).
    # Detection heuristic: if another milestone was added within the last
    # 60 seconds, treat this as bulk-add mode and skip the inline hint.
    # session-start / dispatch warnings still surface the SPEC promotion
    # for any MS still missing a SPEC, so users don't lose the reminder.
    if not _is_bulk_milestone_add(data, ms_id):
        print(
            f"\n  Hint: SPEC (要求書 / 判断軌跡) を作成すると、サブエージェントや retrospection が"
            f"機能しやすくなります。`/beacon-spec {ms_id}` を実行するか、後で session-start で"
            f"warning が出たときに作成してください。"
        )
def _is_bulk_milestone_add(data: dict, current_ms_id: str) -> bool:
    """Return True if another milestone was added within the last 60 seconds.

    Used by cmd_milestone_add to suppress the SPEC promotion hint during
    bulk-add operations (e.g. /beacon-roadmap registering 6 MSs in a row).
    The trigger system still records spec-needed for each MS, and
    session-start surfaces them — only the inline hint is suppressed.
    """
    import datetime as _dt
    try:
        now = _dt.datetime.now(_dt.timezone.utc)
        # ms-143: read Target records through the manifest-driven accessor rather
        # than naming data['milestones'] directly, so this helper (part of the
        # milestone_add verb's reach) is concrete-free. This helper is milestone-
        # specific (bulk-add of milestones), so it asks for the milestone kind
        # explicitly via target_records(data, "milestone") — NOT the all-collections
        # iter_target_records, which would fold a sibling occupation's Targets
        # (opportunities) into the "most recent prior record" scan in a mixed
        # project (maintainability review PR #628 finding §1). Append order within
        # the milestones collection is preserved, so the semantics are unchanged.
        import occupation
        records = occupation.target_records(data, "milestone")
        # Compare against the second-most-recent record (the most recent is the
        # one we just added).
        prior = None
        for ms in reversed(records):
            if ms.get("id") == current_ms_id:
                continue
            prior = ms
            break
        if prior is None:
            return False
        prior_created = prior.get("created_at", "") or ""
        if not prior_created:
            return False
        # ISO format; strip trailing Z and parse
        prior_dt = _dt.datetime.fromisoformat(prior_created.replace("Z", "+00:00"))
        delta = (now - prior_dt).total_seconds()
        return delta < 60.0
    except Exception:
        return False
def cmd_milestone_list():
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    show_all = os.environ.get("BEACON_ALL", "") == "1"
    ms_filter_str = os.environ.get("BEACON_MS_FILTER", "")
    data = load_project()

    milestones = occupation.target_records(data, "milestone")
    if not show_all:
        milestones = [ms for ms in milestones if ms.get("status") != "cancelled"]

    if ms_filter_str:
        ms_ids = [m.strip() for m in ms_filter_str.split(",") if m.strip()]
        all_ids = {ms["id"] for ms in occupation.target_records(data, "milestone")}
        for ms_id in ms_ids:
            if ms_id not in all_ids:
                print(f"Error: milestone not found: {ms_id}", file=sys.stderr)
                sys.exit(1)
        milestones = [ms for ms in milestones if ms["id"] in ms_ids]

    if json_mode:
        output = {
            "name": data.get("name", ""),
            "summary": data.get("summary", ""),
            "profession": occupation.resolve_profession(data),
            # ms-115 e-3788 — 発見性: この職種が作れる target-class を session-start が
            # 読む status に載せる。「この職種で作れるのは X」を最初から見せて、他職種の
            # 対象を誤って作ろうとする前に自然に正しい入口へ導く (封じ込め block は最後の砦)。
            "owned_target_classes": list(
                occupation.OWNED_TARGET_CLASSES.get(
                    occupation.resolve_profession(data), ())),
            # ms-109 e-3751 — class-layer forward-motion frame, so every
            # project's session-start reads "advance the target" inline (a
            # per-project CORE doc would only reach this repo's sessions).
            "target_advancement_frame": work_model.target_advancement_frame(data),
            # ms-108 e-3269 — occupation-agnostic Target projection (③ shared
            # frame). Development still emits the legacy ``milestones[]`` below
            # unchanged (expand step); ``targets[]`` is the canonical view that
            # projects Milestones (dev) OR Opportunities (sales) uniformly, so
            # the shared frame / session-start can read one shape regardless of
            # occupation. Sales projects — whose ``milestones[]`` is empty —
            # finally surface their work here.
            "targets": occupation.project_targets(data),
            # ms-146 e-5339 — the Targets the mechanism thinks should probably be
            # wrapped up (over the declared time budget, or recent evidence says
            # the work stopped moving the objective). Normally an EMPTY list, so
            # /beacon-session-start can raise it the moment it is not: the owner
            # who cannot stop is precisely the one who will not go looking.
            "stop_signals": occupation.stop_signal_rows(data),
            "milestones": [],
            "operations": [],
            # ms-61 / e-1843 — pending Operations (= todo / in_progress)
            # are surfaced separately so /beacon-session-start Step 3.7
            # ("pending Operation の活性化議論") has data to read.
            # The legacy ``operations[]`` field stays open-only so all
            # existing readers / Skills keep working unchanged.
            "pending_operations": [],
        }
        # ms-143: collect into a local list, then assign — avoids reading back
        # ``output["milestones"]`` (a LOAD subscript on the local result dict the
        # collection-coupling checker cannot tell apart from project data).
        _ms_out = []
        for ms in milestones:
            entries = ms.get("entries", [])
            total_tasks, done_tasks = core.count_task_status(entries)
            ms_item = {
                "id": ms["id"],
                "title": work_model.target_label(ms),
                "status": ms.get("status", "todo"),
                "progress": ms.get("progress", 0),
                "target_date": ms.get("target_date", ""),
                "total_tasks": total_tasks,
                "done_tasks": done_tasks,
            }
            for f in ("priority", "objective", "acceptance_criteria", "description"):
                if ms.get(f):
                    ms_item[f] = ms[f]
            _ms_out.append(ms_item)
        output["milestones"] = _ms_out
        import datetime as _dt
        today_str = _dt.date.today().isoformat()
        for op in data.get("operations", []):
            if op.get("status") == "open":
                entries = op.get("entries", [])
                runs = [e for e in entries if e.get("type") == "run_record"]
                open_incidents = [e for e in entries if e.get("type") == "incident" and e.get("status") == "open"]
                recent_runs = []
                for r in runs[-3:]:
                    recent_runs.append({"date": _local_date(r.get("date", "")), "status": r.get("status", "")})
                output["operations"].append({
                    "id": op["id"],
                    "title": work_model.target_label(op),
                    "status": op.get("status", "open"),
                    "log_source": op.get("log_source", ""),
                    "schedule": op.get("schedule", {}),
                    "recent_runs": recent_runs,
                    "open_incidents": open_incidents,
                })
            elif op.get("status") in ("todo", "in_progress"):
                # ms-61 / e-1843 — surface pending Operations for the
                # Skill's Step 3.7 activation discussion. We carry the
                # full ``entries`` list so the Skill / helper can count
                # operation_tasks without a second CLI call (mirrors the
                # "embed enough context to avoid round-trips" pattern
                # used for milestones above).
                entries = op.get("entries", [])
                op_tasks = [e for e in entries if e.get("type") == "operation_task"]
                op_tasks_done = sum(1 for t in op_tasks if t.get("status") == "done")
                output["pending_operations"].append({
                    "id": op["id"],
                    "title": work_model.target_label(op),
                    "status": op.get("status", ""),
                    "log_source": op.get("log_source", ""),
                    "schedule": op.get("schedule", {}),
                    "activation_hint": op.get("activation_hint", ""),
                    "objective": op.get("objective", ""),
                    "operation_tasks_total": len(op_tasks),
                    "operation_tasks_done": op_tasks_done,
                    # The Skill's classifier helper reads ``entries`` to
                    # decide verdict; embed verbatim so caller has one
                    # source of truth.
                    "entries": entries,
                })
        print(json.dumps(output, ensure_ascii=False))
        return

    icons = {"done": "\u25cf", "in_progress": "\u25d1", "todo": "\u25cb",
             "waiting": "\u25cc", "in_review": "\u25d5", "observing": "\u25d5",
             "cancelled": "\u2718"}
    # ms-109 e-3751 \u2014 imprint the forward-motion frame before listing Targets,
    # so the AI reads "advance the target" every time it reads status (mirrors
    # how sales ``phase list`` prints ``\u25a0 \u524d\u9032\u306e\u67a0\u7d44\u307f``). Class-agnostic, so it
    # shows for dev and every occupation alike.
    print(f"\u25a0 \u524d\u9032\u306e\u67a0\u7d44\u307f: {work_model.target_advancement_frame(data)}\n")
    # ms-115 e-3788 \u2014 \u767a\u898b\u6027: \u975e\u958b\u767a\u8077\u7a2e\u3067\u306f\u300c\u3053\u306e\u8077\u7a2e\u3067\u4f5c\u308c\u308b\u5bfe\u8c61\u300d\u3092 1 \u884c\u6dfb\u3048\u308b\u3002
    # \u55b6\u696d\u30e6\u30fc\u30b6\u30fc\u304c milestone \u3092\u63a2\u3057\u3066\u8ff7\u3046 / acquisition \u306e\u5b58\u5728\u306b\u6c17\u3065\u304b\u306a\u3044\u7a74\u3092\u57cb\u3081\u308b\u3002
    _owned = occupation.OWNED_TARGET_CLASSES.get(
        occupation.resolve_profession(data), ())
    if occupation.resolve_profession(data) != "dev" and _owned:
        print(f"  \u3053\u306e\u8077\u7a2e\u3067\u4f5c\u308c\u308b\u5bfe\u8c61: {', '.join(_owned)} "
              f"(\u4ed6\u8077\u7a2e\u306e\u5bfe\u8c61\u306f\u4f5c\u6210\u3067\u304d\u307e\u305b\u3093)\n")
    # ms-108 e-3269 (\u5897\u5206B) \u2014 the human-readable status projects the
    # occupation's Targets. Development keeps its exact line format
    # (Milestone + progress %); sales and other occupations, whose
    # ``milestones[]`` is empty, list their Targets (Opportunities) via the
    # shared projection so ``beacon status`` is no longer blank for them.
    if occupation.resolve_profession(data) == "dev":
        for ms in milestones:
            icon = icons.get(ms["status"], "?")
            active = " \u25c0 ACTIVE" if ms["status"] == "in_progress" else ""
            progress = ms.get("progress", 0)
            print(f"  {icon} [{ms['id']}] {work_model.target_label(ms)} ({progress}%){active}")
    else:
        for t in occupation.project_targets(data):
            # Occupations carry their own status vocabulary (sales: open /
            # won / lost …), so fall back to a neutral bullet rather than the
            # development "?" when a status is not in the dev icon map.
            icon = icons.get(t["status"], "•")
            phase = t.get("detail", {}).get("phase", "")
            phase_note = f" [{phase}]" if phase else ""
            done = t["work_items_done"]
            total = t["work_items_total"]
            print(f"  {icon} [{t['id']}] {t['label']} ({t['status']}){phase_note} "
                  f"{done}/{total}")

    # ms-146 e-5339 — 「そろそろ切り上げでは？」. Printed ONLY when a Target actually
    # trips a declared signal, so its appearance carries information. It reports
    # and stops there: the ms-146 SPEC 設計方針2 ruling is that the mechanism
    # surfaces the reason and the human decides whether to keep going.
    _stops = occupation.stop_signal_rows(data)
    if _stops:
        print("\n■ そろそろ切り上げでは？ (機構からの提示 — 続けるかはあなたの判断です)")
        for _row in _stops:
            print(f"  [{_row['id']}] {_row['label']}")
            for _sig in _row["signals"]:
                print(f"    - {_sig['message']}")


def cmd_milestone_start():
    """Activate an MS, auto-create its workspace, and self-add as assignee.

    Layered behaviour (current):

      1. Self-assignee: the actor (``lib/agent.get_actor()``) is added to
         the MS's assignee list (no-op if already present).
      2. Auto-workspace (ms-65 e-1477, cwd-aware):
           - Main project root → create a worktree at
             ``.worktrees/<ms-branch>/`` checked out to ``ms-XX-<slug>``.
             The main cwd's HEAD is NOT touched. This avoids the
             2026-06-10 incident where two bclaude sessions in the same
             cwd silently shared a git HEAD (per CORE doc *MS 活性化 =
             workspace 確保の根本責務*, see ms-65 SPEC).
           - Inside an existing worktree (e.g. dispatch-spawned context,
             or the user manually cd'd into one) → keep the legacy
             in-place ``git checkout`` behaviour. Switching the worktree's
             own HEAD does not affect other sessions because each worktree
             has its own HEAD.
      3. Skip when not a git repo: dispatch contexts (tests, scaffolds)
         where there is no .git transparently fall back to "just flip
         status" so existing flows are unaffected.

    Suppression knobs:
      * ``BEACON_NO_BRANCH=1`` skips the workspace / branch step entirely
        (used by tests and by callers that manage their own branching).
      * ``BEACON_NO_ASSIGNEE=1`` skips the assignee auto-add.
    """
    ms_id = os.environ.get("BEACON_MS_ID", "")
    no_branch = os.environ.get("BEACON_NO_BRANCH", "") == "1"
    no_assignee = os.environ.get("BEACON_NO_ASSIGNEE", "") == "1"

    data = load_project()
    ms = core.milestone_start(data, ms_id)

    # ---- 1b. occupation claim (ms-81 e-1918) ----
    # Tied to milestone_start so status / assignee / occupation always lift
    # together. If a previous claim exists from another session, warn —
    # never block (= SPEC §3-3, 努力義務). Gated by BEACON_NO_BRANCH /
    # BEACON_NO_ASSIGNEE so test sandboxes and scripted scaffolds that
    # request a pure status flip don't trip session-resolution side effects.
    if not no_branch and not no_assignee:
        try:
            import agent as _agent_for_claim
            actor_for_claim = _agent_for_claim.get_actor()
        except Exception:
            actor_for_claim = {}
        sid_for_claim = _resolve_session_id() or ""
        _ms_claim, previous_claim = core.milestone_claim_occupation(
            data, ms_id,
            session_id=sid_for_claim,
            machine=actor_for_claim.get("machine", ""),
            agent=actor_for_claim.get("agent", ""),
        )
        if previous_claim and previous_claim.get("session_id") != sid_for_claim:
            prev_sid = previous_claim.get("session_id", "?")
            prev_machine = previous_claim.get("machine", "?")
            print(
                f"  [ms-81 occupation] previous claim by session "
                f"{prev_sid[:12]}... on {prev_machine} (claimed_at: "
                f"{previous_claim.get('claimed_at', '?')}). Proceeding with "
                f"takeover; if that session crashed, this is normal — if it is "
                f"still actively working, coordinate via beacon dm.",
                file=sys.stderr,
            )
            core.milestone_record_occupation_event(
                data, ms_id=ms_id, event_type="takeover",
                session_id=sid_for_claim,
                machine=actor_for_claim.get("machine", ""),
                agent=actor_for_claim.get("agent", ""),
                reason=f"superseded session {prev_sid[:12]}",
            )
        else:
            core.milestone_record_occupation_event(
                data, ms_id=ms_id, event_type="claim",
                session_id=sid_for_claim,
                machine=actor_for_claim.get("machine", ""),
                agent=actor_for_claim.get("agent", ""),
            )

    # ---- 1. assignee auto-add (lib/agent.py is the single source) ----
    actor_str = ""
    if not no_assignee:
        try:
            import agent as _agent
            actor = _agent.get_actor()
            # Wire-format: prefer the agent name (it's the unique writer
            # identifier — machine is implicit). If the machine should
            # ever be needed for disambiguation it's still in the commit
            # metadata (e-934). For the assignee badge a clean handle
            # reads better.
            actor_str = actor.get("agent", "") or actor.get("machine", "")
            if actor_str:
                _ms_unused, _added = core.milestone_assignee_add(
                    data, ms_id, actor_str
                )
        except Exception as e:  # pragma: no cover - defensive
            print(f"  warning: could not auto-add assignee: {e}", file=sys.stderr)

    # ---- 2. auto-workspace / auto-branch (cwd-aware + project-type-aware) ----
    # ms-81 e-1917: explicit non-git degrade. If the project isn't a git repo
    # (= research / writing project, scaffold, fresh dir), we skip the entire
    # worktree branch silently and only flip status + assignee. Per CORE doc
    # DqIvAVzDprcq6hsq0AuF §3-2 the physical-boundary mechanism degrades to
    # logical occupation only; for now occupation is recorded server-side
    # (handled in e-1918), so degrade simply means "no worktree".
    branch_name = ""
    branch_msg = ""
    workspace_path = ""
    non_git_skip = False
    if not no_branch:
        if not _is_git_project():
            non_git_skip = True
        else:
            try:
                import branch as _branch
                branch_name = _branch.ms_branch_name(ms_id, ms.get("title", ""))
                if _is_in_main_project_root():
                    import worktree as _worktree
                    workspace_path = os.path.join(".worktrees", branch_name)
                    try:
                        wt = _worktree.create_workspace(workspace_path, branch_name)
                        branch_msg = "worktree created" if wt["created"] else "worktree exists"
                    except _worktree.GitNotInstalledError:
                        # No git → fall through silently (status flip already done).
                        branch_name = ""
                        workspace_path = ""
                    except _worktree.WorktreeCreateError as exc:
                        print(f"  warning: could not create worktree: {exc}", file=sys.stderr)
                        branch_name = ""
                        workspace_path = ""
                else:
                    # Already inside a worktree: in-place checkout is safe
                    # because each worktree owns its own HEAD.
                    branch_msg = _ensure_on_branch(branch_name)
            except _NotAGitRepoError:
                # Quiet skip: scaffolds / tests / fresh projects without git
                # should still be able to start a milestone.
                branch_name = ""
            except Exception as e:  # pragma: no cover - defensive
                print(f"  warning: could not create/switch branch: {e}", file=sys.stderr)

    save_project(data)
    print(f"Activated: {work_model.target_label(ms)}")
    if actor_str and not no_assignee:
        print(f"  assignee: {actor_str}")
    if branch_name and branch_msg:
        print(f"  branch: {branch_name} ({branch_msg})")
    if workspace_path:
        print(f"  next: cd {workspace_path} && bclaude")
        print(f"        (新しいセッションをこの worktree で開いて作業してください — "
              f"同 cwd で並走すると別マイルストーンの作業を同じ branch に書く事故が起きるため)")
    if non_git_skip:
        # ms-81 e-1917: surface the project-type degrade so the user knows
        # the worktree step was intentionally skipped (= research / writing
        # project), not silently dropped.
        print("  workspace: non-git project, worktree step skipped "
              "(logical occupation only)")
def _is_git_project() -> bool:
    """Return True if the current project root looks like a git repository.

    ms-81 e-1917: explicit project-type detection used by ``milestone start``
    to decide whether to engage the worktree mechanism. We check the cheap
    common-case (``.git`` exists at the beacon root / cwd) before falling
    back to ``git rev-parse``, which would otherwise produce noisy stderr
    on plain directories.
    """
    # Cheapest path: a ``.git`` directory or file at cwd / beacon root.
    candidates = [os.getcwd()]
    beacon_root = os.environ.get("BEACON_ROOT", "")
    if beacon_root and beacon_root not in candidates:
        candidates.append(beacon_root)
    for root in candidates:
        if os.path.exists(os.path.join(root, ".git")):
            return True
    # Fall back to git rev-parse (covers the worktree case where ``.git`` is
    # a pointer file outside cwd).
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0
    except (FileNotFoundError, OSError):
        return False
def _is_in_main_project_root() -> bool:
    """Return True if the cwd is the main project root (not inside a worktree).

    Detection: ``git rev-parse --git-dir`` and ``--git-common-dir`` return
    the same path at the main checkout, but in a linked worktree the
    ``--git-dir`` points to ``<main>/.git/worktrees/<name>/`` while
    ``--git-common-dir`` still points to the shared ``<main>/.git``.

    Conservative default: on any failure (git missing, not a repo, parse
    error) return True. Treating "unknown environment" as "main root"
    keeps the cwd-aware path engaged so the worktree creation is at least
    attempted; if git is genuinely missing the worktree helper will raise
    GitNotInstalledError and the caller falls back gracefully.
    """
    try:
        gd = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True, text=True, timeout=5,
        )
        cd = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, OSError):
        return True
    if gd.returncode != 0 or cd.returncode != 0:
        return True
    return os.path.abspath(gd.stdout.strip()) == os.path.abspath(cd.stdout.strip())
class _NotAGitRepoError(RuntimeError):
    """Raised by :func:`_ensure_on_branch` when we are not inside a git repo.

    Distinct from generic ``RuntimeError`` so callers can swallow only this
    case (transparent skip) without hiding real git errors.
    """
def _ensure_on_branch(branch_name: str) -> str:
    """Make sure HEAD is on ``branch_name``; create it from HEAD if missing.

    Returns a short human-readable status string used by the CLI to tell
    the user what happened ("created", "switched", "already on it",
    "preserved", or empty). Raises :class:`_NotAGitRepoError` if the
    working directory is not inside a git repository.

    The function intentionally does NOT touch ``main`` (or any upstream)
    — it creates the new branch *from current HEAD*. Callers that want
    a fresh base from origin/main should run ``git fetch && git
    checkout main`` first; this helper is just the safe local step.
    """
    try:
        # First confirm we're in a git repo. `rev-parse --is-inside-work-tree`
        # exits 0 + "true" on success, non-zero otherwise.
        check = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, OSError) as e:
        raise _NotAGitRepoError("git not available") from e
    if check.returncode != 0 or check.stdout.strip() != "true":
        raise _NotAGitRepoError("not inside a git work tree")

    # Where are we now?
    cur = subprocess.run(
        ["git", "branch", "--show-current"],
        capture_output=True, text=True, timeout=5,
    )
    current = cur.stdout.strip() if cur.returncode == 0 else ""
    if current == branch_name:
        return "already on it"

    # Does the branch already exist locally?
    exists = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet",
         f"refs/heads/{branch_name}"],
        capture_output=True, text=True, timeout=5,
    )
    if exists.returncode == 0:
        # Switch to it; refuse if there are uncommitted changes (let git
        # handle the safety check — we don't want to overwrite work).
        sw = subprocess.run(
            ["git", "checkout", branch_name],
            capture_output=True, text=True, timeout=10,
        )
        if sw.returncode != 0:
            err = sw.stderr.strip() or "checkout failed"
            print(f"  warning: branch {branch_name} exists but checkout failed: {err}",
                  file=sys.stderr)
            return "preserved"
        return "switched"

    # Create it from current HEAD.
    create = subprocess.run(
        ["git", "checkout", "-b", branch_name],
        capture_output=True, text=True, timeout=10,
    )
    if create.returncode != 0:
        err = create.stderr.strip() or "create failed"
        print(f"  warning: could not create {branch_name}: {err}", file=sys.stderr)
        return "preserved"
    return "created"
def _prompt_close_leftover_worktree(ms_id: str, transition: str) -> None:
    """ms-81 e-1919: surface leftover worktrees on phase transitions.

    When an MS moves to done / observing / waiting we check whether the
    branch-specific worktree directory still exists; a leftover worktree
    is the temptation other sessions could later (re-)check out and
    accidentally commit against. We prompt rather than block: interactive
    runs get a [y/N] auto-close; non-interactive runs get a one-line
    warning and proceed. Per SPEC §4 this is intentionally a forcing
    function, not enforcement.
    """
    try:
        data = load_project()
    except Exception:
        return
    ms = next((m for m in occupation.target_records(data, "milestone") if m.get("id") == ms_id), None)
    if ms is None:
        return
    try:
        import branch as _branch
        branch_name = _branch.ms_branch_name(ms_id, ms.get("title", ""))
    except Exception:
        return
    workspace_path = os.path.join(".worktrees", branch_name)
    if not os.path.exists(workspace_path):
        return
    msg = (
        f"\n[ms-81 transition prompt] worktree still present at "
        f"{workspace_path} after {transition} of [{ms_id}].\n"
        f"   Leftover worktrees can be (re-)entered by other sessions; "
        f"cleanup keeps the audit trail tight."
    )
    print(msg, file=sys.stderr)
    if not sys.stdin.isatty():
        print(
            "   (non-interactive — leaving the worktree in place; clean up "
            "with `beacon milestone workspace-cleanup " + ms_id + "` when "
            "convenient.)",
            file=sys.stderr,
        )
        return
    try:
        choice = input("   Auto-close worktree? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        choice = "n"
    if choice in ("y", "yes"):
        # Defer to the existing workspace_cleanup command rather than
        # duplicating the git worktree remove machinery; it already
        # handles branch checks and idempotency.
        os.environ["BEACON_MS_ID"] = ms_id
        cmd_milestone_workspace_cleanup()
    else:
        print(
            f"   leaving worktree in place; run `beacon milestone "
            f"workspace-cleanup {ms_id}` to remove later.",
            file=sys.stderr,
        )
# ms-119 / e-4008 — completion-gate refusal messages (done / observe share the
# same 目的達成 gate, so their AI-direct-completion refusals share one shape and
# differ only in the verb wording; {ms_id} is filled per call). Kept as module
# constants so the two verbs cannot drift in what escape hatches they advertise.
_DONE_BAN_ERROR = (
    "Error: completing {ms_id} directly (without the review gate) from "
    "an AI session is refused (ms-119 / e-4008 structural guard).\n"
    "  『構造発火・非迂回』requires the completion gate to be "
    "non-bypassable for AI sessions — a bare `done` skipped the review.\n"
    "  Paths forward (= one of these):\n"
    "    1. beacon milestone done {ms_id} --review — route through the "
    "目的達成 gate (AI assembles evidence, human approves).\n"
    "    2. BEACON_TARGET_COMPLETE_USER_OVERRIDE=1 — explicit user opt-in "
    "for a one-off straight completion.\n"
    "    3. BEACON_SESSION_KIND=human — declare the calling session is "
    "human-driven (= straight terminal use)."
)
_OBSERVE_BAN_ERROR = (
    "Error: moving {ms_id} to observing directly (without the review gate) "
    "from an AI session is refused (ms-119 / e-4008 structural guard).\n"
    "  observing は『基本目的は達成済み・運用に回してよい』という完了主張なので、"
    "done と同じくゲートを迂回できません (observe で素通り着地する抜け穴を塞ぐ)。\n"
    "  Paths forward (= one of these):\n"
    "    1. beacon milestone observe {ms_id} --review — route through the "
    "目的達成 gate (AI assembles evidence, human approves).\n"
    "    2. BEACON_TARGET_COMPLETE_USER_OVERRIDE=1 — explicit user opt-in "
    "for a one-off straight observe.\n"
    "    3. BEACON_SESSION_KIND=human — declare the calling session is "
    "human-driven (= straight terminal use)."
)
def _milestone_status(data: dict, ms_id: str) -> str:
    """Current status string of a milestone, or "" if not found."""
    for _m in occupation.target_records(data, "milestone"):
        if _m.get("id") == ms_id:
            return _m.get("status", "")
    return ""
def _completion_gate_or_route(data: dict, ms_id: str, *, old_state: str,
                              new_state: str, reason: str, ban_error: str) -> bool:
    """Shared completion-gate for milestone done / observe (ms-119).

    A milestone completion claim (-> done/closed, or -> observing from a work
    state) must pass the 目的達成 gate. This centralizes the two rules that MUST
    stay identical across done/observe so they cannot drift:
      1. --review (BEACON_REVIEW=1) routes to the human-approval gate (e-3912).
      2. an AI session's direct (non-review) completion is refused (e-4008).
    Routine transitions that assert no completion (e.g. todo -> observing, or a
    done -> observing re-open) are NOT gated and pass straight through.

    Returns True if the caller should STOP (routed to the review gate); False if
    the caller should apply the transition directly (routine, or the ban passed
    for a human / override session).
    """
    if not _ta.is_attainment_transition("milestone", old_state, new_state):
        return False  # not a completion claim → apply directly, no gate
    if os.environ.get("BEACON_REVIEW", "") == "1":
        _route_completion_to_review(data, ms_id, reason=reason,
                                    new_state=new_state)
        return True
    if _ai_session_direct_completion_ban_active():
        print(ban_error.format(ms_id=ms_id), file=sys.stderr)
        sys.exit(2)
    return False
def cmd_milestone_done():
    ms_id = os.environ.get("BEACON_MS_ID", "")
    reason = _require_reason_or_skip("milestone done")
    data = load_project()
    # ms-119 e-3912/e-4008: --review routes to the 目的達成 gate; a bare AI-session
    # completion is refused (non-bypassable). Routine/human paths pass through.
    old_state = _milestone_status(data, ms_id)
    if _completion_gate_or_route(data, ms_id, old_state=old_state,
                                 new_state="done", reason=reason,
                                 ban_error=_DONE_BAN_ERROR):
        return
    ms = core.milestone_done(data, ms_id, reason=reason)
    _release_occupation_for_transition(data, ms_id, reason="done")
    save_project(data, op={"op": "milestone_done", "ms_id": ms_id, "reason": reason})
    # ms-119 e-3911: this completion did NOT go through the review gate — fire a
    # review-due nudge (目的達成 toward the gate + 思想 if the MS has a SPEC).
    _fire_review_due_trigger(ms_id, "milestone", old_state, "done",
                             target_title=work_model.target_label(ms),
                             has_spec=_spec_exists_for_ms(ms_id), gated=False)
    _prompt_close_leftover_worktree(ms_id, "done")
    print(f"Completed: {work_model.target_label(ms)}")
    if reason:
        print(f"  Reason: {reason}")
def _route_completion_to_review(data: dict, ms_id: str, *, reason: str,
                                new_state: str = "done") -> None:
    """Route a milestone completion through the 目的達成レビュー gate instead of
    applying it directly (ms-119 e-3912 opt-in path).

    ``new_state`` is the completion-claim target state: "done" for `milestone
    done --review`, "observing" for `milestone observe --review` (observing is a
    completion claim here — see lib/transition_approval docstring)."""
    try:
        target = core._find_approval_target(data, ms_id)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    old_state = target.get("status", "")
    if not _ta.requires_spine_approval("milestone", old_state, new_state):
        print(
            f"Error: {ms_id} の {old_state} → {new_state} は目的達成レビュー対象外です "
            f"(完了主張の遷移ではありません)。",
            file=sys.stderr)
        sys.exit(1)
    eid = core.target_transition_approval_add(
        data, ms_id, old_state=old_state, new_state=new_state,
        intent=reason, actor=_actor_str(),
        session_id=_resolve_session_id() or "")
    save_project(data, op={"op": "target_transition_approval_add",
                           "target_id": ms_id, "entry_id": eid})
    # ms-119 e-3911: the 目的達成 review is already in-flight (the pending
    # approval), so fire only the 思想 advisory here (gated=True suppresses the
    # attainment nudge) — and only if the MS has a SPEC 原典 to check against.
    _fire_review_due_trigger(ms_id, "milestone", old_state, new_state,
                             target_title=target.get("title", ""),
                             has_spec=_spec_exists_for_ms(ms_id), gated=True)
    print(f"目的達成レビュー依頼を作成: {eid}")
    print(f"  {ms_id}: {old_state} -> {new_state} (完了主張、人間承認待ち)")
    if reason:
        print(f"  intent: {reason}")
    # ms-119 e-3911 §5 AC6: weak-AC gap surfacing (hard-block しない)
    _gap = _ta.format_criteria_gap(
        _ta.assess_completion_criteria(
            has_spec=_spec_exists_for_ms(ms_id),
            objective=target.get("objective", ""),
            acceptance=target.get("acceptance_criteria", ""),
            intent=reason),
        target_id=ms_id)
    if _gap:
        print(_gap)
    # ms-119 e-4579: surface the unstarted highest/high backlog up front, so the
    # approver knows the disposition table must be filled before approve will pass.
    _bgap = _ta.format_backlog_gap(
        core.unstarted_priority_tasks(target), target_id=ms_id,
        spec_updated_at=_spec_updated_at_for_target(ms_id))
    if _bgap:
        print(_bgap)
    _print_evidence_guidance(eid, ms_id)
    print(f"  確定 (= 遷移実行): beacon target approve {eid} [--rationale <text>]")
    print(f"  却下 (= 遷移せず): beacon target reject {eid} [--rationale <text>]")
def cmd_milestone_wait():
    """Transition a milestone to ``waiting`` status (ms-81 e-1915).

    Requires --reason (same gate as observe / done) so retro can reconstruct
    why work paused. The transition is rejected by core.milestone_wait if
    the source status is not active or observing.
    """
    ms_id = os.environ.get("BEACON_MS_ID", "")
    reason = _require_reason_or_skip("milestone wait")
    if not ms_id:
        print("Usage: beacon milestone wait <ms-id> --reason <text>",
              file=sys.stderr)
        sys.exit(1)
    data = load_project()
    try:
        ms = core.milestone_wait(data, ms_id, reason=reason)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    _release_occupation_for_transition(data, ms_id, reason="wait")
    save_project(data, op={"op": "milestone_wait", "ms_id": ms_id,
                           "reason": reason})
    _prompt_close_leftover_worktree(ms_id, "wait")
    print(f"Waiting: [{ms['id']}] {work_model.target_label(ms)}")
    if reason:
        print(f"  Reason: {reason}")
def cmd_milestone_occupations():
    """List worktree_sessions / occupation log entries (ms-81 e-1921).

    Surfaces the audit trail recorded by milestone_record_occupation_event.
    Filtered with --ms <ms-id> to scope to one milestone; --json emits the
    raw shape for downstream tools (= Web UI audit tab in a later iteration
    can hit this endpoint via the standard project sync rather than a new
    subcollection plumbing).
    """
    ms_filter = os.environ.get("BEACON_MS_ID", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()
    log = data.get("worktree_sessions", [])
    if ms_filter:
        log = [e for e in log if e.get("ms_id") == ms_filter]
    if json_mode:
        print(json.dumps(log, ensure_ascii=False))
        return
    if not log:
        print("(no occupation events recorded)")
        return
    for ev in log:
        ev_type = ev.get("event_type", "?")
        ev_ms = ev.get("ms_id", "?")
        ev_sid = (ev.get("session_id") or "?")[:14]
        ev_machine = ev.get("machine", "")
        ev_agent = ev.get("agent", "")
        ev_at = ev.get("at", "")
        ev_reason = ev.get("reason", "")
        actor_str = f"{ev_machine}/{ev_agent}" if ev_machine or ev_agent else "?"
        line = f"  {ev_at[:19]} [{ev_type:8}] {ev_ms:6} by {ev_sid}... ({actor_str})"
        if ev_reason:
            line += f"  — {ev_reason}"
        print(line)
def cmd_milestone_release():
    """Release the occupation claim on a milestone without changing status
    (ms-81 e-1918, SPEC AC #16).

    Use this when finishing a working session on an active MS so the next
    session can pick it up immediately. The MS stays in its current phase;
    only the per-session occupation marker is cleared.
    """
    ms_id = os.environ.get("BEACON_MS_ID", "")
    if not ms_id:
        print("Usage: beacon milestone release <ms-id>", file=sys.stderr)
        sys.exit(1)
    data = load_project()
    try:
        _ms, released = core.milestone_release_occupation(
            data, ms_id, reason="manual"
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if released:
        sid = _resolve_session_id() or ""
        try:
            import agent as _agent_for_release
            actor = _agent_for_release.get_actor()
        except Exception:
            actor = {}
        core.milestone_record_occupation_event(
            data, ms_id=ms_id, event_type="release",
            session_id=sid,
            machine=actor.get("machine", ""),
            agent=actor.get("agent", ""),
            reason="manual",
        )
        save_project(data, op={"op": "milestone_release", "ms_id": ms_id})
        prev_sid = released.get("session_id", "?")
        print(
            f"Released: [{ms_id}] (was claimed by session "
            f"{prev_sid[:12] if prev_sid else '?'}...)"
        )
    else:
        print(f"Released: [{ms_id}] (was not occupied; no-op)")
def cmd_milestone_observe():
    """Transition a milestone to ``observing`` status with --reason gating
    (e-976).

    Previously `beacon milestone observe` dispatched to ``milestone_update``
    with ``BEACON_STATUS=observing`` and accepted an optional reason. With
    the gate, observing is now a first-class transition verb that must record
    why the work paused (so retro can reconstruct the decision). The reason
    is forwarded through ``core.milestone_update`` and ends up at
    ``meta.observing_reason``, identical to the previous path; only the gate
    is new.
    """
    ms_id = os.environ.get("BEACON_MS_ID", "")
    reason = _require_reason_or_skip("milestone observe")
    if not ms_id:
        print("Usage: beacon milestone observe <ms-id> --reason <text> [--review]",
              file=sys.stderr)
        sys.exit(1)
    data = load_project()
    # ms-119: observing is a completion claim (運用改善フェーズ = 基本目的達成が
    # 前提。目的達成 / 思想の逸脱があれば運用に回さない = observe しない) when it
    # comes from a work state, so it passes the same 目的達成 gate as done. The
    # shared helper handles --review routing + the e-4008 AI-direct ban, and lets
    # routine transitions (e.g. todo -> observing) through ungated.
    old_state = _milestone_status(data, ms_id)
    if _completion_gate_or_route(data, ms_id, old_state=old_state,
                                 new_state="observing", reason=reason,
                                 ban_error=_OBSERVE_BAN_ERROR):
        return
    try:
        ms = core.milestone_update(data, ms_id, status="observing",
                                   reason=reason)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    _release_occupation_for_transition(data, ms_id, reason="observe")
    save_project(data, op={"op": "milestone_observe", "ms_id": ms_id,
                           "reason": reason})
    # ms-119: fire the review-due nudge (目的達成 + 思想 if SPEC), mirroring done.
    # For a routine old_state (todo/done -> observing) the binding is empty, so
    # nothing fires — consistent with the gate above passing it through.
    _fire_review_due_trigger(ms_id, "milestone", old_state, "observing",
                             target_title=work_model.target_label(ms),
                             has_spec=_spec_exists_for_ms(ms_id), gated=False)
    _prompt_close_leftover_worktree(ms_id, "observe")
    print(f"Observing: [{ms['id']}] {work_model.target_label(ms)}")
    if reason:
        print(f"  Reason: {reason}")
def cmd_milestone_join():
    """Add the current actor as an assignee on an MS (ms-51 / e-933).

    Mirror of the assignee-add step of ``ms start``, but without flipping
    status (the MS is presumed already active or otherwise in a healthy
    state). With ``--checkout`` (BEACON_CHECKOUT=1) the MS branch is also
    switched-to, with a fetch-from-origin fallback if the branch only
    exists on the remote.

    Per SPEC AC-3, duplicate joins are a warned no-op rather than a
    hard error — joining a board you're already on is a benign user
    intent ("am I on this?") and a hard error would be hostile.
    Per SPEC AC-4, done / cancelled milestones refuse the join; that
    rejection is raised by core.milestone_assignee_add.
    """
    ms_id = os.environ.get("BEACON_MS_ID", "")
    do_checkout = os.environ.get("BEACON_CHECKOUT", "") == "1"

    if not ms_id:
        print("Usage: beacon ms join <ms-id> [--checkout]", file=sys.stderr)
        sys.exit(1)

    data = load_project()

    # Find the MS up front so we can read its title (needed for slug + the
    # human-facing "joined X" message) and so we can fail fast with a
    # clearer error than "Milestone not found" from the deep helper.
    ms = next((m for m in occupation.target_records(data, "milestone") if m.get("id") == ms_id), None)
    if ms is None:
        print(f"Error: Milestone not found: {ms_id}", file=sys.stderr)
        sys.exit(1)

    # Resolve the actor name through the same lib/agent.py contract used
    # by ms start; keeps the two paths trivially consistent.
    import agent as _agent
    actor = _agent.get_actor()
    actor_str = actor.get("agent", "") or actor.get("machine", "")
    if not actor_str:
        print("Error: could not resolve agent identity (lib/agent.py)",
              file=sys.stderr)
        sys.exit(1)

    try:
        _ms, added = core.milestone_assignee_add(data, ms_id, actor_str)
    except ValueError as e:
        # done / cancelled MS or other validation failure — surface as 1.
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    save_project(data)

    if added:
        print(f"Joined: [{ms_id}] {ms.get('title','')} as {actor_str}")
    else:
        print(f"Already on [{ms_id}] as {actor_str} (no-op)")

    # ---- optional --checkout ----
    if not do_checkout:
        return

    try:
        import branch as _branch
        branch_name = _branch.ms_branch_name(ms_id, ms.get("title", ""))
    except Exception as e:
        print(f"  warning: could not derive branch name: {e}", file=sys.stderr)
        return

    try:
        # 1. Try a local-only ensure first. If the branch is here we just
        #    switch; nothing else to do.
        msg = _ensure_on_branch(branch_name)
        print(f"  branch: {branch_name} ({msg})")
        return
    except _NotAGitRepoError:
        print("  warning: not in a git repo; --checkout skipped",
              file=sys.stderr)
        return
    except Exception:
        # Fall through to remote fetch attempt.
        pass

    # 2. Local create failed (most likely the branch only exists on origin).
    #    Try to fetch + checkout the remote branch.
    fetch = subprocess.run(
        ["git", "fetch", "origin", branch_name],
        capture_output=True, text=True, timeout=30,
    )
    if fetch.returncode != 0:
        print(f"  warning: fetch origin {branch_name} failed; "
              f"branch may not exist yet on remote", file=sys.stderr)
        return
    co = subprocess.run(
        ["git", "checkout", "-B", branch_name,
         f"origin/{branch_name}"],
        capture_output=True, text=True, timeout=10,
    )
    if co.returncode == 0:
        print(f"  branch: {branch_name} (tracked from origin)")
    else:
        print(f"  warning: checkout {branch_name} failed: "
              f"{co.stderr.strip()}", file=sys.stderr)
def cmd_milestone_show():
    ms_id = os.environ.get("BEACON_MS_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()

    for ms in occupation.target_records(data, "milestone"):
        if ms["id"] == ms_id:
            entries = ms.get("entries", [])
            total_tasks, done_tasks = core.count_task_status(entries)
            if json_mode:
                output = {
                    "id": ms["id"],
                    "title": work_model.target_label(ms),
                    "description": ms.get("description", ""),
                    "status": ms.get("status", "todo"),
                    "progress": ms.get("progress", 0),
                    "target_date": ms.get("target_date", ""),
                    "total_tasks": total_tasks,
                    "done_tasks": done_tasks,
                    "entries": core.entries_to_json(entries),
                }
                for f in ("priority", "objective", "acceptance_criteria"):
                    if ms.get(f):
                        output[f] = ms[f]
                print(json.dumps(output, ensure_ascii=False))
            else:
                icons = {"done": "\u25cf", "in_progress": "\u25d1", "todo": "\u25cb",
                         "waiting": "\u25cc", "in_review": "\u25d5", "cancelled": "\u2718"}
                icon = icons.get(ms["status"], "?")
                print(f"{icon} [{ms['id']}] {work_model.target_label(ms)}")
                if ms.get("description"):
                    print(f"  {ms['description']}")
                print(f"  Status: {ms['status']}  Progress: {ms.get('progress', 0)}%")
                print(f"  Target: {ms.get('target_date') or '-'}")
                print(f"  Tasks: {done_tasks}/{total_tasks} done")
            return

    print(f"Milestone not found: {ms_id}")
    sys.exit(1)
def cmd_milestone_update():
    ms_id = os.environ.get("BEACON_MS_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    status = os.environ.get("BEACON_STATUS", "")
    reason = os.environ.get("BEACON_REASON", "")

    # e-630: changing MS status or owner/assignee is a decision worth
    # recording. Require --reason so the changelog has a "why", not just
    # a "what". Other fields (title, description, etc.) are content edits
    # and don't need a reason — those are equivalent to documentation
    # updates and would just create noise if forced.
    decision_fields = bool(
        status
        or os.environ.get("BEACON_OWNER", "")
        or os.environ.get("BEACON_ASSIGNEE", "")
    )
    if decision_fields and not reason:
        print(
            "Error: --reason is required when changing status / owner / "
            "assignee. These are team-visible decisions and the decision "
            "trail must record the 'why' (CORE doc data-immutability-principle).",
            file=sys.stderr,
        )
        print(
            "  Example: beacon milestone update ms-42 --status observing "
            "--reason 'merge after ms-43 lands'",
            file=sys.stderr,
        )
        sys.exit(1)

    data = load_project()
    try:
        ms = core.milestone_update(
            data, ms_id,
            title=os.environ.get("BEACON_TITLE", ""),
            progress=os.environ.get("BEACON_PROGRESS", ""),
            target_date=os.environ.get("BEACON_TARGET_DATE", ""),
            status=status,
            description=os.environ.get("BEACON_DESCRIPTION", ""),
            reason=reason,
            priority=os.environ.get("BEACON_PRIORITY", ""),
            objective=os.environ.get("BEACON_OBJECTIVE", ""),
            acceptance_criteria=os.environ.get("BEACON_ACCEPTANCE_CRITERIA", ""),
            owner=os.environ.get("BEACON_OWNER", ""),
            assignee=os.environ.get("BEACON_ASSIGNEE", ""),
        )
    except ValueError as e:
        print(str(e))
        sys.exit(1)
    changelog_op = {"op": f"milestone_{status}", "ms_id": ms_id, "reason": reason} if status else None
    save_project(data, op=changelog_op)
    if json_mode:
        print(json.dumps({"id": ms["id"], "title": work_model.target_label(ms),
                          "status": ms["status"], "progress": ms.get("progress", 0)},
                         ensure_ascii=False))
    else:
        print(f"Updated: [{ms['id']}] {work_model.target_label(ms)}")
def cmd_milestone_delete():
    ms_id = os.environ.get("BEACON_MS_ID", "")
    reason = os.environ.get("BEACON_REASON", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not reason:
        print("Error: --reason is required for milestone delete.")
        print("  Example: beacon milestone delete <ms-id> --reason \"スコープアウト: 別MSに統合\"")
        sys.exit(1)
    data = load_project()
    try:
        ms = core.milestone_delete(data, ms_id, reason=reason)
    except ValueError as e:
        print(str(e))
        sys.exit(1)
    save_project(data, op={"op": "milestone_delete", "ms_id": ms_id, "reason": reason})
    if json_mode:
        print(json.dumps({"id": ms["id"], "status": "cancelled"}, ensure_ascii=False))
    else:
        print(f"Cancelled: [{ms['id']}] {work_model.target_label(ms)}")
        print(f"  Reason: {reason}")
def cmd_milestone_purge():
    """Hard-delete a milestone record (Issue #14 recovery path).

    Unlike `beacon milestone delete` (soft / status=cancelled), purge
    physically removes the record from the array. This exists only for
    data-corruption recovery and is not a substitute for soft delete.

    Loads via load_project_unsafe so it can recover from a project that
    already fails validation (the whole point of the command). The save
    path also bypasses validation when residual duplicates remain so
    the operator can purge them one at a time; once clean, validation
    passes naturally on the next normal load.

    Cloud mode (e-1030): routes through the server `POST .../purge` endpoint
    which enforces owner-only access. Editors get a clear 403 instead of
    silently mutating the local cache and pushing.
    """
    ms_id = os.environ.get("BEACON_MS_ID", "")
    reason = os.environ.get("BEACON_REASON", "")
    index_str = os.environ.get("BEACON_INDEX", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not ms_id:
        print("Error: ms-id is required.", file=sys.stderr)
        print(
            "  Usage: beacon milestone purge <ms-id> --reason \"...\" [--index <n>]",
            file=sys.stderr,
        )
        sys.exit(1)
    if not reason:
        print(
            "Error: --reason is required for milestone purge "
            "(audit trail per CORE doc data-immutability-principle).",
            file=sys.stderr,
        )
        print(
            "  Example: beacon milestone purge ms-13 "
            "--reason \"duplicate ID — Issue #14 recovery\" --index 2",
            file=sys.stderr,
        )
        sys.exit(1)
    index: Optional[int] = None
    if index_str:
        try:
            index = int(index_str)
        except ValueError:
            print(f"Error: --index must be an integer, got '{index_str}'.",
                  file=sys.stderr)
            sys.exit(1)

    # ms-84 Phase 2 (e-2036): Store.purge_milestone unifies the cloud + local
    # paths. The CLI no longer branches on ``_is_cloud_mode()``; the Store
    # implementation knows how to talk to its backend (cloud server enforces
    # owner-only access + post-purge validation; LocalStore does the local
    # file mutation + still_dirty bookkeeping). Pre-flight stays here as a
    # UX layer (= friendly duplicate display before delegating).
    store = get_store()
    try:
        data = store.load_project()
    except (RuntimeError, ConnectionError) as e:
        print(f"Error loading project: {e}", file=sys.stderr)
        sys.exit(1)
    matches = core.find_milestones(data, ms_id)
    if not matches:
        print(f"Milestone not found: {ms_id}", file=sys.stderr)
        sys.exit(1)
    if len(matches) > 1 and index is None:
        print(
            f"Milestone '{ms_id}' has {len(matches)} duplicate records. "
            "Re-run with --index <n>:",
            file=sys.stderr,
        )
        for i, m in enumerate(matches, 1):
            title = m.get("title", "(no title)")
            status = m.get("status", "?")
            print(f"  --index {i}  status={status}  title={title}",
                  file=sys.stderr)
        sys.exit(1)

    try:
        result = store.purge_milestone(ms_id, reason=reason, index=index)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    purged = result["purged"]
    still_dirty = result["still_dirty"]
    dup_report = result["dup_report"]

    # Local-mode changelog parity: the cloud server records its own audit
    # trail via operations.apply_operation, so we only append to
    # .beacon/changelog.jsonl when LocalStore did the mutation.
    if not store.is_cloud():
        _append_changelog({
            "op": "milestone_purge",
            "ms_id": ms_id,
            "index": index,
            "reason": reason,
            "purged_title": purged.get("title", ""),
        })

    if json_mode:
        out = {
            "id": purged["id"],
            "title": purged.get("title", ""),
            "purged": True,
            "still_dirty": still_dirty,
        }
        print(json.dumps(out, ensure_ascii=False))
    else:
        print(f"Purged: [{purged['id']}] {purged.get('title', '')}")
        print(f"  Reason: {reason}")
        if still_dirty:
            remaining: list[str] = []
            for category, dupes in dup_report.items():
                for did, n in dupes.items():
                    remaining.append(f"{category[:-1]} '{did}' x{n}")
            print("  Note: residual duplicates remain — "
                  + ", ".join(remaining))
            print("  Run `beacon doctor` to inspect and purge the next one.")
def cmd_milestone_depends():
    ms_id = os.environ.get("BEACON_MS_ID", "")
    depends_on_str = os.environ.get("BEACON_DEPENDS_ON", "")
    clear = os.environ.get("BEACON_CLEAR", "") == "1"
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not ms_id:
        print("Error: milestone ID is required", file=sys.stderr)
        sys.exit(1)

    data = load_project()
    if clear:
        ms = core.milestone_depends(data, ms_id, [])
    else:
        if not depends_on_str:
            print("Error: --on is required (or use --clear)", file=sys.stderr)
            sys.exit(1)
        deps = [d.strip() for d in depends_on_str.split(",") if d.strip()]
        ms = core.milestone_depends(data, ms_id, deps)
    save_project(data)

    if json_mode:
        print(json.dumps({"id": ms["id"], "depends_on": ms.get("depends_on", [])}, ensure_ascii=False))
    else:
        deps = ms.get("depends_on", [])
        if deps:
            print(f"{ms_id} depends on: {', '.join(deps)}")
        else:
            print(f"{ms_id}: dependencies cleared")
def cmd_milestone_workspace():
    # OSS: git worktree lifecycle core
    # Human Executor notification (beacon trigger fire) is handled below and is closed-source.
    #
    # ms-81 e-1917: this verb is being demoted to a deprecated alias of
    # `milestone start`. The common "create worktree + flip status" path
    # belongs on `start` so status / assignee / worktree always lift
    # together. Two legacy sub-modes survive here for back-compat:
    #   - `--clear`: legacy workspace-field clear (kept; pure data op)
    #   - `--dir <path>` (BEACON_NO_GIT=1): legacy explicit-path mode that
    #     bypasses git worktree creation entirely (kept; some scripts use it)
    # The default no-arg path emits a deprecation warning and delegates to
    # `cmd_milestone_start` so callers stop accumulating drift.
    import subprocess
    from datetime import datetime, timezone

    ms_id = os.environ.get("BEACON_MS_ID", "")
    workspace = os.environ.get("BEACON_WORKSPACE", "")
    clear = os.environ.get("BEACON_CLEAR", "") == "1"
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    executor = os.environ.get("BEACON_EXECUTOR", "")  # ai | human | human:<user>
    no_git = os.environ.get("BEACON_NO_GIT", "") == "1"  # skip git worktree add (for --dir mode)

    if not ms_id:
        print("Error: milestone ID is required", file=sys.stderr)
        sys.exit(1)

    data = load_project()

    # ms-81 e-1917: deprecation alias. If neither legacy sub-mode is in play
    # (= no --clear, no --dir/no-git), the caller is using the default
    # worktree-create path which has moved to `milestone start`.
    if not clear and not no_git:
        print(
            "[ms-81 deprecation] `beacon milestone workspace` is being "
            "absorbed by `beacon milestone start` so that status / assignee "
            "/ worktree always activate together. Forwarding to "
            "`milestone start` now; please update callers to use "
            "`beacon milestone start " + ms_id + "` directly.",
            file=sys.stderr,
        )
        cmd_milestone_start()
        return

    if clear:
        # Legacy --clear: remove the old workspace field only
        ms = core.milestone_workspace(data, ms_id, "")
        save_project(data)
        if json_mode:
            print(json.dumps({"id": ms["id"], "workspace": ms.get("workspace")}, ensure_ascii=False))
        else:
            print(f"{ms_id}: workspace cleared")
        return

    if workspace and no_git:
        # Legacy --dir mode: set workspace path without git worktree
        ms = core.milestone_workspace(data, ms_id, workspace)
        save_project(data)
        if json_mode:
            print(json.dumps({"id": ms["id"], "workspace": ms.get("workspace")}, ensure_ascii=False))
        else:
            print(f"{ms_id} workspace: {workspace}")
        return

    # OSS: git worktree add + milestone field update
    # Determine worktree path and branch
    workspace_branch = f"{ms_id}/work"
    workspace_path = os.path.join(".worktrees", ms_id)

    # ms-65 e-1476: worktree creation now lives in lib/worktree.py so the
    # upcoming cwd-aware ``beacon milestone start`` (e-1477) can share the
    # same retry-on-existing-branch behaviour. The shape of the legacy CLI
    # output (stderr line on idempotent reuse, exit 1 on failure) is
    # preserved so existing dispatch flows don't see a change.
    import worktree as _worktree
    try:
        wt = _worktree.create_workspace(workspace_path, workspace_branch)
    except _worktree.GitNotInstalledError:
        print("Error: git not found in PATH", file=sys.stderr)
        sys.exit(1)
    except _worktree.WorktreeCreateError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    if not wt["created"]:
        print(f"Worktree already exists at {workspace_path}", file=sys.stderr)

    assigned_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    effective_executor = executor or "ai"

    ms = core.milestone_worktree_set(
        data, ms_id,
        workspace_branch=workspace_branch,
        workspace_path=workspace_path,
        executor=effective_executor,
        executor_assigned_at=assigned_at,
    )
    # Also update legacy workspace field for backward compatibility
    ms = core.milestone_workspace(data, ms_id, workspace_path)
    save_project(data)

    # Closed-source: Human Executor notification via beacon trigger
    if effective_executor.startswith("human"):
        user_part = effective_executor[len("human:"):] if ":" in effective_executor else ""
        user_label = f" ({user_part})" if user_part else ""
        msg = (
            f"ブランチ {workspace_branch} をチェックアウトして作業開始: "
            f"git checkout {workspace_branch}"
        )
        try:
            subprocess.run(
                ["beacon", "trigger", "fire", f"{ms_id}-workspace", msg],
                capture_output=True, text=True
            )
        except FileNotFoundError:
            pass  # beacon not in PATH; skip trigger

    if json_mode:
        print(json.dumps({
            "id": ms_id,
            "workspace_branch": workspace_branch,
            "workspace_path": workspace_path,
            "executor": effective_executor,
            "executor_assigned_at": assigned_at,
        }, ensure_ascii=False))
    else:
        print(f"{ms_id} worktree: {workspace_path}  branch: {workspace_branch}  executor: {effective_executor}")
def cmd_milestone_workspace_cleanup():
    # OSS: git merge + git worktree remove lifecycle cleanup
    import subprocess

    ms_id = os.environ.get("BEACON_MS_ID", "")
    merge_to = os.environ.get("BEACON_MERGE_TO", "")  # target branch for merge
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not ms_id:
        print("Error: milestone ID is required", file=sys.stderr)
        sys.exit(1)

    data = load_project()
    ms = next((m for m in occupation.target_records(data, "milestone") if m["id"] == ms_id), None)
    if ms is None:
        print(f"Error: milestone not found: {ms_id}", file=sys.stderr)
        sys.exit(1)

    workspace_path = ms.get("workspace_path") or ms.get("workspace")
    if not workspace_path:
        print(f"Error: {ms_id} has no workspace_path set", file=sys.stderr)
        sys.exit(1)

    abs_workspace_path = os.path.abspath(workspace_path)
    workspace_branch = ms.get("workspace_branch", "")

    errors = []

    # Step 1: git merge (OSS: git worktree lifecycle)
    if merge_to:
        # Merge workspace branch into the specified branch
        if workspace_branch:
            merge_result = subprocess.run(
                ["git", "merge", workspace_branch],
                capture_output=True, text=True,
                cwd=os.path.abspath(".")  # run from project root
            )
        else:
            merge_result = subprocess.run(
                ["git", "merge", abs_workspace_path],
                capture_output=True, text=True
            )
        if merge_result.returncode != 0:
            errors.append(f"git merge failed: {merge_result.stderr.strip()}")

    # Step 2: git worktree remove (OSS: git worktree lifecycle)
    if os.path.exists(abs_workspace_path):
        rm_result = subprocess.run(
            ["git", "worktree", "remove", abs_workspace_path, "--force"],
            capture_output=True, text=True
        )
        if rm_result.returncode != 0:
            errors.append(f"git worktree remove failed: {rm_result.stderr.strip()}")

    # Step 3: clear milestone worktree fields
    core.milestone_worktree_clear(data, ms_id)
    # Also clear legacy workspace field
    core.milestone_workspace(data, ms_id, "")
    save_project(data)

    if json_mode:
        print(json.dumps({
            "id": ms_id,
            "cleaned_up": True,
            "errors": errors,
        }, ensure_ascii=False))
    else:
        if errors:
            for e in errors:
                print(f"Warning: {e}", file=sys.stderr)
        print(f"{ms_id}: workspace cleaned up (path: {workspace_path})")
def cmd_milestone_graph():
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    data = load_project()
    graph = core.milestone_graph(data)

    if json_mode:
        print(json.dumps(graph, ensure_ascii=False))
    else:
        for wave_info in graph["waves"]:
            wave_num = wave_info["wave"]
            cycle_marker = " [CYCLE]" if wave_info.get("cycle") else ""
            ms_ids = wave_info["milestones"]
            # Build display lines
            lines = []
            for ms_id in ms_ids:
                node = next((n for n in graph["nodes"] if n["id"] == ms_id), None)
                if node:
                    deps = node.get("depends_on", [])
                    dep_str = f" <- {', '.join(deps)}" if deps else ""
                    status_icon = {"done": "●", "in_progress": "◐", "todo": "○",
                                   "waiting": "◌", "observing": "◔"}.get(node["status"], "?")
                    lines.append(f"  {status_icon} {ms_id} {work_model.target_label(node)} ({node['progress']}%){dep_str}")
            print(f"Wave {wave_num}{cycle_marker}:")
            for line in lines:
                print(line)
def _fire_spec_needed_trigger(ms_id: str, ms_title: str) -> None:
    """Promote SPEC creation for a newly-added milestone.

    Writes a trigger file that session-start / dispatch will surface as a
    warning. This is a *soft* promotion: never blocks. The trigger is
    cleared when a SPEC doc is added for the MS (see _spec_exists_for_ms
    in trigger_check).
    """
    if not ms_id:
        return
    # If SPEC already exists for this MS, no need to fire.
    if _spec_exists_for_ms(ms_id):
        return
    triggers_dir = _get_triggers_dir()
    os.makedirs(triggers_dir, exist_ok=True)
    trigger_path = os.path.join(triggers_dir, f"spec-needed-{ms_id}.json")
    if os.path.exists(trigger_path):
        return
    import datetime
    trigger_data = {
        "name": f"spec-needed-{ms_id}",
        "kind": "spec-needed",
        "ms_id": ms_id,
        "ms_title": ms_title,
        "message": f"{ms_id} \"{ms_title}\" に SPEC (要求書/判断軌跡) がありません。"
                   f"`/beacon-spec {ms_id}` で作成すると、サブエージェントや retrospection が機能しやすくなります。",
        "created_at": datetime.datetime.now().isoformat(),
    }
    with open(trigger_path, "w", encoding="utf-8") as f:
        json.dump(trigger_data, f, ensure_ascii=False)
        f.write("\n")
