"""Trek ("/api/treks/*") router — collaborative "缶詰" work-room endpoints.

ms-127 e-4870 (B フェーズ): the trek endpoints extracted from the
server/app.py god-module, following the make_router(require_auth, *,
...injected helpers) pattern established by routers_me / routers_orgs /
routers_admin / routers_auth.

Pure move: every route body and helper is verbatim from app.py's trek
handlers — same paths, same shapes, no behaviour change. Two mechanical
deltas only: ``@app.<m>`` decorators become ``@router.<m>``, and reads of
app.py's module-global ``_auth_enabled`` bool become calls to the injected
``is_auth_enabled()`` getter (so tests that flip the shared flag at runtime
are reflected — same reasoning as routers_auth's get_local_dev_enabled).

Boundary with app.py — INTENTIONALLY LEFT BEHIND:
  ``POST /api/system/trek-scheduler/tick`` (trek_scheduler_tick_endpoint)
  stays in app.py. It is not purely trek: it also drives the Operation
  scheduler (``_fire_due_scheduled``) and shares a time-source invariant
  with operation firing (a 2026-07-19 refactor silently killed operation
  firing here — see the comment block around that endpoint). Extracting it
  is a separate, higher-risk slice (follow-up e-4870b). The tick re-imports
  the handful of trek helpers it still needs (the module-level helpers
  below) via ``from routers_treks import ...``.

Module-level helpers (below, importable by app.py's scheduler tick) are the
self-contained ones that depend only on ``db`` / ``trek_mod`` /
``envelope_mod`` — no auth flag, no injected app.py callables. Everything
else (auth-reading guards + all route handlers) is nested inside
``make_router`` so it can close over the injected dependencies.

Injected dependencies (owned by app.py, passed in to avoid an import cycle):

- ``require_auth``            — the host app's identity dependency.
- ``_load``                   — ``app._load`` (project load + role check).
- ``_require_admin``          — ``app._require_admin`` (instance-admin gate).
- ``_resolve_author``         — ``app._resolve_author`` (task-add authorship).
- ``_apply_op_and_broadcast`` — ``app._apply_op_and_broadcast`` (write + WS
                                broadcast path, used by task-add).
- ``is_auth_enabled``         — zero-arg callable returning app.py's current
                                ``_auth_enabled`` bool (getter, not snapshot).

``db`` mirrors app.py's binding — ``store_router as db`` (e-1544 backend routing).
"""
from __future__ import annotations

import logging
import sys
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import store_router as db  # e-1544: same backend-routing binding app.py uses
import trek as trek_mod
import envelope as envelope_mod
import decision_event as decision_event_mod
import dm_gate as dm_gate_mod
import core
import work_model


# Blocker-op error kind -> HTTP status (verbatim from app.py; used by
# add_trek_blocker_endpoint below). Module-level so the nested route reads it
# as a closure-free global.
_BLOCKER_ERROR_STATUS = {
    "cycle": 409,
    "not_blockable": 409,
    "self_block": 400,
    "missing_id": 400,
}


# ---------------------------------------------------------------------------
# Pydantic request models (trek-only; verbatim from app.py)
# ---------------------------------------------------------------------------

class TrekCreate(BaseModel):
    title: str
    description: str = ""
    type: str = "persistent"  # temporary | persistent
    creator_session_id: str   # caller's session_id (becomes leader)
    # ms-83 / e-1994: optional cadence + future-form manager URL at creation
    # time. Both are recorded on ``meta``; cadence falls back to default
    # (= 10 minutes) when omitted, manager_agent_url is unused in this MS.
    cadence_minutes: Optional[int] = None
    manager_agent_url: Optional[str] = None

class TrekUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    type: Optional[str] = None
    # ms-83 / e-1994 — periodic cadence (= server-side "next, please" DM
    # interval in minutes) and a future-form manager-agent URL slot.
    # Both live on ``meta`` so the on-disk shape stays tidy. ``None``
    # leaves the field unchanged; explicit empty string / explicit 0
    # behaviours are handled in the setter functions.
    cadence_minutes: Optional[int] = None
    manager_agent_url: Optional[str] = None

class TrekInvite(BaseModel):
    email: str

class TrekScopeOp(BaseModel):
    project: str
    milestone: Optional[str] = None
    operation: Optional[str] = None
    task: Optional[str] = None
    opportunity: Optional[str] = None
    account: Optional[str] = None

class TrekSlotAdd(BaseModel):
    project: str
    milestone: Optional[str] = None
    operation: Optional[str] = None
    task: Optional[str] = None
    opportunity: Optional[str] = None
    account: Optional[str] = None
    included_task_ids: Optional[list[str]] = None

class TrekSlotAmend(BaseModel):
    add_children: list[str] = []
    remove_children: list[str] = []

class TrekSlotClaim(BaseModel):
    session_id: str = ""  # empty string = unclaim gesture (SPEC 方針 4)

class TrekTaskStateSet(BaseModel):
    """ms-75 / e-2048 — Trek-internal task state declaration.

    ``task_id`` is the project entry id (e-XXXX) the executor is stamping.
    ``state`` is one of the canonical 5-state set ``block``, ``todo``,
    ``working``, ``leader_review``, ``user_review`` (legacy aliases migrate on
    write: ``done`` → ``user_review``, ``waiting-review`` → ``leader_review``;
    ``block`` is set only via the blocker endpoint, not here). ``note`` is a
    short freeform string (≤500 chars) attached to the state record so the
    leader review surface can show executor rationale without a separate DM
    round-trip.
    """
    task_id: str
    state: str
    note: Optional[str] = ""
    # ms-128 / e-4386 — 完遂ゲート (attainment mode)。leader が完成レビューで選んだ
    # verdict 名 (``approve`` / ``re-work`` / ``forward-to-user``)。executor が直接
    # stamp する時は空。``user_review`` へ倒す時の合格判定を分岐するのに使う
    # (approve = attainment gate 対象 / forward-to-user = 人間エスカレーションで gate 外)。
    verdict: Optional[str] = ""
    # SPEC 受入条件を criterion 単位で評価した構造化 verdict。
    # ``[{"criterion": str, "verdict": "met"|"partial"|"not-met"}, ...]``。
    # ``approve`` で user_review に倒すには全 met が必須 (= 実行者の外の judge が出す)。
    attainment_verdict: Optional[list] = None

class TrekBlockerSet(BaseModel):
    """ms-128 方針4 (e-4365) — draw blocker edges target_id → blocker_target_ids.

    The leader records that ``target_id`` depends on each id in
    ``blocker_target_ids`` (= cannot proceed until the blocker is 取り込み済み).
    All edges are applied **atomically** in one request (server-side all-or-
    nothing) so cloud and local CLI have the same partial-failure semantics
    (AX/maintainability review 2026-07-29). ``blocker_target_id`` (singular) is a
    back-compat alias folded into the list.
    """
    target_id: str
    blocker_target_ids: List[str] = []
    blocker_target_id: Optional[str] = None
    note: Optional[str] = ""

class TrekUnblockSet(BaseModel):
    """ms-128 方針4 (e-4365) — remove a blocker edge target_id → blocker_target_id.

    The leader's manual escape hatch (break a cycle by dropping one edge). After
    removal the server reconciles the target's block state.
    """
    target_id: str
    blocker_target_id: str

class TrekHaltSet(BaseModel):
    issued_by_session_id: str
    reason: str = ""
    # ms-90 / e-3241: 中断の判断理由 (= なぜ止めると決めたか)。reason は「直面
    # した問題」= context に、rationale は「その判断の理由」に分けて記録する。任意。
    rationale: str = ""

class TrekTaskAddRequest(BaseModel):
    target_project: str
    target_milestone: str
    description: str
    type: str = "task"
    priority: str = ""
    motivation: str = ""
    acceptance_criteria: str = ""

class TrekTransferLeader(BaseModel):
    from_session_id: str  # current leader session (caller's session)
    to_session_id: str    # new leader session

class TrekTakeOver(BaseModel):
    session_id: str  # 新 leader_session_id (= 呼び出し session の sid)

class TrekPulseAck(BaseModel):
    session_id: str
    picked_choice: str = ""  # 5-choice token, see lib/trek.VALID_PULSE_PICKED_CHOICES (ms-88 / e-2139)
    note: str = ""
    # Structured fields — see lib/trek.record_pulse_ack docstring for shape.
    state_summary: str = ""
    blockers: list[str] = []
    needs_leader_judgment: bool = False
    time_on_task_seconds: int = 0

class TrekKickoff(BaseModel):
    session_id: str
    kickoff_dm_event_id: str = ""  # bus.send 結果の event_id (= audit trace)

class TrekSuccessionConsent(BaseModel):
    decision: str  # "accept" | "decline"

class TrekExtendTtl(BaseModel):
    task_id: str
    minutes: int  # 0 or negative clears the extension
    reason: str = ""  # short audit string (e.g. "dispatched to subagent X")

class TrekBlanketCategory(BaseModel):
    category: str  # See lib/trek._normalised_blanket_category for shape

class TrekReconcileRequest(BaseModel):
    apply: bool = False

class TrekHeartbeatRequest(BaseModel):
    """Body for POST /api/treks/{trek_id}/session-heartbeat (ms-83 / e-2001).

    The AI session inside the trek's leader (or any member) pings this
    endpoint after each tick completes, so the server's idle detector
    knows the session is alive. Stamps ``meta.last_session_response_at``
    on the trek doc.

    Identity check: caller must be a joined member of the trek.
    """
    session_id: str = ""


# ---------------------------------------------------------------------------
# Module-level trek helpers (self-contained: db / trek_mod / envelope_mod).
# Importable by app.py's scheduler-tick (which stays behind). Verbatim.
# ---------------------------------------------------------------------------

def _resolve_canonical_project_id(
    maybe: str, *, user_id: str
) -> Optional[str]:
    """Resolve a slug or full project_id to its canonical full project_id.

    ms-95 / Trek task-add cross-project bug: scope entries can be stored
    using the user-friendly **slug** (= just ``"profile-extractor"``)
    because that's what users type at the CLI (``beacon trek plan
    --add-scope profile-extractor:ms-5``). But ``operations.apply_operation``
    requires the **full project_id** (= ``"profile-extractor-276d28"`` with
    the 6-char md5 path-hash suffix the CLI mints in ``cmd_cloud_setup``).
    Without this resolver, the task-add endpoint passes the slug down to
    ``db.get_project`` → returns None → ``LookupError`` → uncaught → HTTP
    500. This helper canonicalizes both the request side and (caller's
    copy of) the scope side so the scope match works on equal footing and
    the downstream apply_operation call always sees the full id.

    Returns:
      * the full project_id on **unique** match
      * ``None`` if not found OR ambiguous (= multiple slug expansions)

    Resolution strategy:
      1. Fast path — assume ``maybe`` is already the full id. If
         ``db.get_project(maybe)`` returns a doc, return ``maybe`` as-is.
      2. Slug path — scan projects accessible to ``user_id`` and pick
         those whose project_id starts with ``f"{maybe}-"`` (= slug prefix
         used by ``cmd_cloud_setup`` when minting ids as ``<slug>-<hex6>``).
         Return the id when exactly one matches, else ``None``.

    Failure modes (intentional, returned as ``None`` so the caller picks
    the right HTTP code):
      * Project does not exist → 404 candidate
      * Multiple projects share the slug prefix → 409 candidate (the
        caller should surface "ambiguous slug" so the user can supply
        the full id explicitly)

    Performance: slug path is O(N) over the user's projects per call.
    No cache — keep the code simple; if this shows up in latency traces
    later, memoize per-request.
    """
    if not maybe:
        return None
    # Fast path: maybe is already a full project_id.
    try:
        if db.get_project(maybe) is not None:
            return maybe
    except Exception:  # noqa: BLE001 - db hiccup falls through to slug path
        pass
    if not user_id:
        # Cannot scope the slug scan without a user — refuse rather than
        # leak cross-tenant ids.
        return None
    try:
        rows = db.list_projects(user_id=user_id)
    except Exception:  # noqa: BLE001 - on db failure, refuse to guess
        return None
    prefix = f"{maybe}-"
    matches = [
        r.get("project_id") for r in (rows or [])
        if (r.get("project_id") or "").startswith(prefix)
    ]
    if len(matches) == 1:
        return matches[0]
    return None

def _canonicalise_trek_scope_projects_in_place(trek_doc: dict) -> None:
    """Rewrite ``trek_doc["scope"][*]["project"]`` to the canonical full
    project_id when the scope entry carries a slug.

    ms-99 / e-2833 — the Phase 2 scheduler's tick decision predicates
    read the slot inventory via ``materialize_slots(trek_doc, get_project=...)``,
    which calls ``get_project`` with each scope entry's ``project``
    value verbatim. If a CLI stored the slug (= ``profile-extractor``
    rather than ``profile-extractor-276d28``), ``db.get_project`` returns
    None and every MS slot resolves to ``("todo", "unstamped")`` —
    silently misfiring both the leader digest gate and the aggregate-
    terminal quiesce path. This helper closes the gap by resolving each
    scope entry against the leader user's project list and rewriting
    the ``project`` field to the full id in place before the tick's
    predicates run.

    No-op for scope entries whose project already resolves via the
    fast path (= ``db.get_project`` returns non-None). Unresolvable
    slugs are left untouched so downstream code observes the exact
    graceful-degradation behaviour pre-fix.
    """
    scope = trek_doc.get("scope") or []
    if not scope:
        return
    leader_user_id = ""
    for m in trek_doc.get("members") or []:
        if (m.get("role") or "") == "leader" and m.get("user_id"):
            leader_user_id = m.get("user_id") or ""
            break
    if not leader_user_id:
        return
    for entry in scope:
        raw = (entry.get("project") or "").strip()
        if not raw:
            continue
        resolved = _resolve_canonical_project_id(raw, user_id=leader_user_id)
        if resolved and resolved != raw:
            entry["project"] = resolved

def _resolve_trek_scope_project_ids(trek_doc: dict) -> list[str]:
    """Resolve every project in ``trek_doc.scope[]`` to canonical full ids.

    ms-95 / e-2639 — Trek scheduler tick was migrated to per-member dm
    fanout (= ms-97 SPEC AC16 / 中心原則 6 「Wake 経路は DM と完全同一」)。
    The single-project ``_resolve_trek_target_project_id`` helper that
    resolved only ``scope[0]['project']`` was removed: it forced the tick
    to post into one project bus, leaving members whose home project sat
    in ``scope[1..N]`` permanently deaf to Trek progress-check / leader-
    digest events. The new helper canonicalises *every* unique project
    listed in scope so the caller can walk all candidate home buses when
    fanning a dm out to each member's live session.

    Resolution strategy mirrors the retired single-project helper: each
    project value is canonicalised through the leader user's project
    access list (= leader is guaranteed to have access to every project
    in scope, so this is the safe identity for scheduler-side slug
    expansion when no end-user request context exists). Slugs that
    cannot be resolved are returned as-is so downstream code can still
    attempt list_sessions / append_bus_event against the raw value —
    matching the pre-e-2639 graceful-degradation behaviour.

    Behaviour:
      * Returns canonical full project_ids in scope order, de-duplicated.
      * Unresolvable slugs are returned raw (= we never raise; the
        scheduler loop must continue across treks).
      * Empty scope returns ``[]``.
    """
    scope = trek_doc.get("scope") or []
    if not scope:
        return []
    leader_user_id = ""
    for m in trek_doc.get("members") or []:
        if (m.get("role") or "") == "leader" and m.get("user_id"):
            leader_user_id = m.get("user_id") or ""
            break
    seen: set[str] = set()
    out: list[str] = []
    for entry in scope:
        raw = (entry.get("project") or "").strip()
        if not raw:
            continue
        canonical = raw
        if leader_user_id:
            resolved = _resolve_canonical_project_id(
                raw, user_id=leader_user_id,
            )
            if resolved:
                canonical = resolved
        if canonical in seen:
            continue
        seen.add(canonical)
        out.append(canonical)
    return out

def _resolve_leader_home_project_id(trek_doc: dict) -> str:
    """Resolve the project whose bus a *leader-addressed* Trek DM must land on.

    ms-97 P4 (= review finding Trek-H2): three leader-bound DMs — the quiesce
    notice, the ``trek-task-review`` request, and the auto-stall notice — were
    posted to ``scope[0]['project']``. That silently drops the DM whenever the
    leader's home project is a *different* scope project (= cross-project Trek),
    because a leader's bridge only subscribes to its own project's bus. The
    quiesce path made this worse by stamping ``quiesce_notified_at`` on a send
    that never reached the leader (= ms-99 AC12 "silent quiesce eliminated"
    broke; same shape as the 2026-06-28 dogfood e-2706).

    Resolution walks each scope project's session registry and returns the
    project that holds the stamped ``leader_session_id``. Falls back to the
    first scope project when the leader session is not found in any registry
    (= leader home outside scope / planning-era trek with no live leader),
    which is the same known limitation the leader-digest resolver carries
    (review M5) and matches the pre-P4 ``scope[0]`` behaviour exactly.

    We walk the *raw* scope project values (no canonicalisation) on purpose:
    the pre-P4 code used the raw ``scope[0]['project']`` too, so the fallback
    is byte-for-byte identical, and we avoid adding a ``db.get_project`` slug
    round-trip to hot paths (= the ``trek-task-review`` PATCH endpoint fires
    this on every review-trigger transition; the e-2650 slot-done precondition
    overreach test pins that non-done transitions stay read-light).
    """
    seen: set[str] = set()
    raw_pids: list[str] = []
    for entry in trek_doc.get("scope") or []:
        pid = (entry.get("project") or "").strip()
        if pid and pid not in seen:
            seen.add(pid)
            raw_pids.append(pid)
    leader_sid = trek_doc.get("leader_session_id") or ""
    if leader_sid:
        for pid in raw_pids:
            try:
                sessions = db.list_sessions(pid)
            except Exception:
                # A single project's registry read failing must not abort the
                # walk — the leader may still be registered in another scope
                # project. Fall through to the next candidate.
                continue
            for s in sessions:
                if (s.get("session_id") or "") == leader_sid:
                    return pid
    return raw_pids[0] if raw_pids else ""

def emit_leader_succession_consent_dm(
    trek_doc: dict,
    candidate_session_id: str,
    *,
    former_leader_session_id: str = "",
    deadline_seconds: int = 1800,
    candidate_home_project_id: str = "",
) -> Optional[str]:
    """Post a 1 hop leader succession consent DM to ``candidate_session_id``.

    ms-97 / Phase 6 (AC15) — structural placeholder for the (Phase 7) AC22
    auto-succession algorithm. AC22 itself is not written here; this helper
    exists so AC22 can call into a stable, signed-envelope DM emit path
    without re-deriving routing / envelope / payload concerns.

    Contract:

    * Payload kind is ``trek-leader-succession-consent``.
    * Required payload fields: ``trek_id``, ``candidate_session_id``,
      ``deadline_seconds``, ``former_leader_session_id``.
    * Envelope is T1-system scoped to the trek, authorizing the single
      action ``trek.leader_succession_consent``.
    * Bus event is posted to ``candidate_home_project_id`` if provided,
      otherwise the first ``trek_doc.scope[].project`` is used (= the
      candidate's working project is what the bus inbox watches).
    * Delivery mode is ``auto-execute`` so the candidate session wakes
      via the bus-armed loop and the user's terminal Claude surfaces the
      DM through the normal /beacon-dm-respond path (= 1 hop consent).
    * Returns the bus event id, or ``None`` if no target bus is
      resolvable (= empty scope and no explicit override) — the caller
      can degrade gracefully without a thrown exception.

    Raises ``ValueError`` only for callable-misuse cases (= empty
    ``candidate_session_id``), so the AC22 algorithm gets a loud signal
    if it forgets to plumb the candidate routing through.
    """
    if not candidate_session_id:
        raise ValueError(
            "candidate_session_id is required (= the session that the "
            "consent DM is addressed to)"
        )
    trek_id = trek_doc.get("trek_id") or ""

    target_project_id = candidate_home_project_id or ""
    if not target_project_id:
        scope = trek_doc.get("scope") or []
        if scope:
            first = scope[0] if isinstance(scope[0], dict) else {}
            target_project_id = first.get("project") or ""
    if not target_project_id:
        # No bus to post to. Caller (= AC22) can fall back to a different
        # candidate or escalate to user. Return None rather than throwing
        # because routing failure is an expected runtime state, not a
        # caller bug.
        return None

    try:
        env = envelope_mod.issue_t1_system_envelope(
            project_id=target_project_id,
            trek_id=trek_id,
            actions_authorized=["trek.leader_succession_consent"],
            data_class="free",
            ttl_seconds=max(int(deadline_seconds), 60),
        )
    except ValueError:
        env = None

    payload = {
        "kind": "trek-leader-succession-consent",
        "trek_id": trek_id,
        "candidate_session_id": candidate_session_id,
        "deadline_seconds": int(deadline_seconds),
        "former_leader_session_id": former_leader_session_id or "",
        "recipient_session_id": candidate_session_id,
        "created_at": trek_mod.utcnow_iso(),
        "body": (
            f"[Trek leader succession consent] trek_id={trek_id}\n"
            "現 leader session が不応状態に陥ったため、 あなたが次期 leader "
            "候補として選ばれました。\n"
            f"deadline: {int(deadline_seconds)} 秒以内に accept / decline を "
            "明示してください。\n"
            "辞退した場合、 次の candidate に escalate されます。"
        ),
    }
    bus_data = {
        "channel": "dm",
        "sender_session_id": "",
        "recipient_session_id": candidate_session_id,
        "payload": payload,
        "envelope": env,
        "delivery": "auto-execute",
        "created_at": trek_mod.utcnow_iso(),
    }
    try:
        return db.append_bus_event(target_project_id, bus_data)
    except Exception:
        # Best-effort delivery; AC22 will observe a missing event via
        # downstream check and re-escalate to the next candidate.
        return None

def _fanout_welcome_ticks_for_pending_members(
    *, trek_doc: dict, trek_id: str,
    scope_project_ids: list[str],
) -> None:
    """ms-97 / e-2637 — Scheduler-side welcome tick safety net.

    Walks ``trek_doc.members[]`` (phase A+ only) and fires a one-shot
    welcome tick for every session whose stamp is missing
    (= ``should_fire_welcome_tick`` True). On success, stamps
    ``meta.welcome_tick_fired_at[session_id]``.

    The join endpoint fires the welcome tick at join time as the primary
    path. This scheduler-side sweep is the safety net that catches:
      * Sessions that joined before this code was deployed.
      * Sessions whose join-time fire failed (network / DB hiccup).
      * Sessions whose welcome tick was lost in a bus replay.

    The trek_doc is mutated in place; the surrounding tick loop's
    save_trek persists the stamps alongside any other mutations made on
    this tick.
    """
    members = trek_doc.get("members") or []
    if not members:
        return
    # ms-97 / e-2637 — Leader session is excluded from welcome tick.
    # The leader created the trek (= already knows about it); welcome
    # tick is for fresh joiners who need the AC28 manual + kickoff
    # primer. Stamping the leader implicitly here would also produce
    # noise (= leader receives an unnecessary dm on every fresh deploy).
    leader_sid = trek_doc.get("leader_session_id") or ""
    for m in members:
        msid = m.get("session_id") or ""
        if not msid:
            continue
        if leader_sid and msid == leader_sid:
            # Stamp the leader so future ticks skip immediately, but
            # do NOT fire a dm to them.
            trek_mod.mark_welcome_tick_fired(trek_doc, session_id=msid)
            continue
        if (m.get("role") or "") == "leader":
            trek_mod.mark_welcome_tick_fired(trek_doc, session_id=msid)
            continue
        if not trek_mod.should_fire_welcome_tick(trek_doc, session_id=msid):
            continue
        event_id = _fire_welcome_tick(
            trek_doc=trek_doc, trek_id=trek_id, session_id=msid,
            scope_project_ids=scope_project_ids,
        )
        if event_id:
            trek_mod.mark_welcome_tick_fired(trek_doc, session_id=msid)

def _fire_welcome_tick(*, trek_doc: dict, trek_id: str,
                       session_id: str,
                       scope_project_ids: list[str] | None = None) -> str:
    """ms-97 / e-2637 — Post a one-shot welcome tick to the joiner's home bus.

    Resolution: walk ``trek_doc.scope`` for the first project whose
    session registry contains ``session_id`` (= same approach as
    ``_build_executor_targets_session_grain``). If no scope project
    surfaces the session, fall back to ``scope[0]`` so the event is
    still written somewhere observable (= the bridge layer's dm
    delivery filter keys on ``recipient_session_id``, so as long as
    the receiver is on this bus the message lands).

    Returns the event_id on success, empty string on failure. Failures
    must never break the join transaction — the welcome tick is a
    bootstrap optimisation, not a load-bearing path.
    """
    if scope_project_ids is None:
        try:
            scope_project_ids = _resolve_trek_scope_project_ids(trek_doc)
        except Exception:
            scope_project_ids = []
    if not scope_project_ids:
        return ""
    target_pid = ""
    for pid in scope_project_ids:
        try:
            project_sessions = db.list_sessions(pid)
        except Exception:
            project_sessions = []
        for s in project_sessions:
            if (s.get("session_id") or "") == session_id:
                target_pid = pid
                break
        if target_pid:
            break
    if not target_pid:
        # Best-effort fallback: deliver to scope[0]. The bridge filters
        # by recipient_session_id so cross-project posts are safe.
        target_pid = scope_project_ids[0]
    payload = trek_mod.build_welcome_tick_payload(
        trek_doc, session_id=session_id,
    )
    try:
        env = envelope_mod.issue_t1_system_envelope(
            project_id=target_pid,
            trek_id=trek_id,
            actions_authorized=["trek.welcome"],
            data_class="free",
            ttl_seconds=3600,
        )
    except Exception:
        env = None
    bus_data = {
        "channel": "dm",
        "sender_session_id": "",
        "recipient_session_id": session_id,
        "payload": payload,
        "envelope": env,
        "delivery": "auto-execute",
        "created_at": trek_mod.utcnow_iso(),
    }
    try:
        return db.append_bus_event(target_pid, bus_data)
    except Exception as exc:
        print(
            f"warn[ms-97 e-2637]: welcome tick append failed for trek "
            f"{trek_id} session {session_id}: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return ""

def _append_trek_log_safe(trek_id: str, entry: dict) -> None:
    """Append a trek log entry, swallowing any backend error.

    Logs are observability, not a transactional concern — a failure to
    persist the log row must never break the original write (= tick /
    pulse-ack / task-state). Errors are printed to stderr for visibility.
    """
    try:
        db.append_trek_log(trek_id, entry)
    except Exception as exc:
        print(
            f"warn[ms-97 e-2603]: append_trek_log failed for trek "
            f"{trek_id}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


def make_router(
    require_auth: Callable,
    *,
    _load: Callable,
    _require_admin: Callable,
    _resolve_author: Callable,
    _apply_op_and_broadcast: Callable,
    is_auth_enabled: Callable[[], bool],
) -> APIRouter:
    """Build the /api/treks/* router with the host app's auth + write helpers.

    Keyword-only + Callable-typed injected helpers with a construction-time
    callability check (same rationale as the sibling routers): a mis-wire fails
    at mount rather than at request time.
    """
    for _name, _dep in (
        ("require_auth", require_auth),
        ("_load", _load),
        ("_require_admin", _require_admin),
        ("_resolve_author", _resolve_author),
        ("_apply_op_and_broadcast", _apply_op_and_broadcast),
        ("is_auth_enabled", is_auth_enabled),
    ):
        if not callable(_dep):
            raise TypeError(
                f"routers_treks.make_router: {_name} must be callable, "
                f"got {type(_dep).__name__} — pass a function, not a value."
            )

    router = APIRouter()

    # ---- nested trek helpers (close over is_auth_enabled / injected deps) ----

    def _trek_find_member_dual(
        t: dict, *, user_id: str, session_id: str = "",
    ) -> dict | None:
        """Phase-gated member lookup (= ms-97 / e-2658 Phase 1 AC6 dual-mode).

        Phase A+ trek (= members[] が session_id keyed) で caller の session_id
        が分かっている時は session-grain で lookup。 caller_sid が空 (= 旧
        bridge / curl など header 未送) の時、 または pre-A trek の時は
        legacy user_id grain で lookup。 dual-mode 経路として cutover 期間
        の互換性を保つ。
        """
        if session_id and trek_mod.is_session_id_keyed(t):
            m = trek_mod.find_member(t, session_id=session_id)
            if m is not None:
                return m
            # Fallback to user_id grain in case the calling session predates
            # the migration (= header set but session not yet registered).
            # This avoids hard-locking honest callers out during the cutover
            # window; AC13 (= strict session_id hard-check) lands separately.
        if not user_id:
            return None
        return trek_mod.find_member(t, user_id=user_id)

    def _load_trek_for_read(
        trek_id: str, user: dict, request: "Request | None" = None,
    ) -> dict:
        """Load a trek doc. 404 if missing, 403 if caller is neither creator
        nor a member (per SPEC visibility = creator OR members).

        ms-97 / e-2658 Phase 1 (AC6) — ``request`` から ``X-Beacon-Session``
        を取り、 phase A+ trek の時は session_id grain で membership check
        する dual-mode 経路。 ``request`` 省略時は user_id grain のみ。
        """
        t = db.get_trek(trek_id)
        if t is None:
            raise HTTPException(status_code=404, detail=f"Trek '{trek_id}' not found")
        if not is_auth_enabled():
            return t
        uid = user.get("sub", "")
        creator_uid = (t.get("creator_actor") or {}).get("user_id", "")
        if creator_uid == uid:
            return t
        caller_sid = ""
        if request is not None:
            caller_sid = request.headers.get("X-Beacon-Session", "") or ""
        if _trek_find_member_dual(t, user_id=uid, session_id=caller_sid) is not None:
            return t
        raise HTTPException(status_code=403, detail="Not a member of this trek")

    def _reject_if_trek_archived(t: dict) -> None:
        """Return 410 Gone when the Trek has been archived (ms-95 / e-2875).

        Defense-in-depth guard for write endpoints. After a Trek is archived
        the server tick stops firing new progress-check events, but events
        already in the bus (fired shortly before archive) can still reach
        executor sessions. Without this guard, executors invoke Skills that
        POST pulse-ack / task-state / task-add to the archived Trek, which
        then generates peer DM traffic and keeps the executor loop alive for
        several minutes after the leader has left the room. The
        client-side inbox-hook drops archived-trek events before Skill
        invocation on updated bridges; this server guard closes the same
        hole for pre-fix bridges (= two layers, either alone suffices).

        Applied to state-mutating executor / leader endpoints:
        pulse-ack, task-state, task-add, kickoff, extend-ttl,
        session-heartbeat. Read endpoints and the archive transition itself
        (= mutates status → archived) are exempt.

        Reference: 2026-07-03 tk-29a11d2f archive dogfood, 5-10 minutes of
        residual peer DM noise after archive; SPEC e-2875 Done when.
        """
        if t.get("status") == "archived":
            raise HTTPException(
                status_code=410,
                detail=(
                    f"Trek '{t.get('trek_id') or ''}' is archived; "
                    f"writes rejected (ms-95 / e-2875)"
                ),
            )

    def _trek_member_role(
        t: dict, user_id: str, session_id: str = "",
    ) -> str:
        """Return the caller's role ('leader' / 'member' / '' if not a member).

        ms-97 / e-2658 Phase 1 — phase A+ trek の時は session_id grain 優先、
        pre-A trek または session_id 不明時は user_id grain。 同 user_id で
        複数 session が居る場合 (= phase A+ silent expand 状況) は session_id
        grain hit が真値、 user_id grain は best-effort first-match。
        """
        member = _trek_find_member_dual(
            t, user_id=user_id, session_id=session_id,
        )
        if member is None:
            return ""
        return member.get("role", "member")

    def _require_trek_leader(
        t: dict, user: dict, request: "Request | None" = None,
    ) -> None:
        """Raise 403 if caller does not hold the leader role on this trek.

        ms-97 / e-2658 Phase 1 — original user_id grain (+ phase A+ で session
        grain 優先) の role check entry-point。 Phase 4 (= AC13 hard-check) で
        leader-only endpoints は ``_require_trek_leader_session`` 経由に切り
        替わるが、 この helper 自身は role check 限定の従来 contract を維持
        する (= 並列で残しておくと既存 caller の影響範囲を最小化できる)。
        """
        if not is_auth_enabled():
            return
        sid = ""
        if request is not None:
            sid = request.headers.get("X-Beacon-Session", "") or ""
        if _trek_member_role(t, user.get("sub", ""), sid) != "leader":
            raise HTTPException(status_code=403, detail="Trek leader role required")

    def _require_trek_leader_session(
        t: dict, user: dict, request: "Request | None" = None,
    ) -> None:
        """Raise 403 unless caller is BOTH the leader role AND the live leader session (ms-97 / AC13).

        Two-layer check (= role at user grain + session at session grain):

          1. ``_require_trek_leader`` — caller has ``role == "leader"`` in
             the trek's members[]. Survives session restart of the same
             user.
          2. session_id hard-check    — caller's ``X-Beacon-Session`` header
             equals ``trek.leader_session_id``. Blocks a second session of
             the same user from impersonating leader actions (= the gap
             that AC13 closes; pre-Phase-4 the role check alone allowed
             any session of the leader user to mutate).

        Layer 2 only fires on phase A+ trek (= ``is_session_id_keyed``
        True). Pre-A trek invariance: the helper degrades to the
        role-only check, matching the legacy contract. Without the
        ``X-Beacon-Session`` header (= legacy CLI / smoke test) we also
        fall through to role-only — the header is opt-in, not a hard
        requirement, so we don't break callers that pre-date the
        session-grain key.

        The 403 detail surfaces a session prefix on both sides of the
        mismatch so the operator can tell "wrong-session of leader user"
        from "non-leader user" at a glance (= dogfood ergonomics).
        """
        _require_trek_leader(t, user, request)
        if not is_auth_enabled():
            return
        if request is None:
            return
        caller_sid = request.headers.get("X-Beacon-Session", "") or ""
        if not caller_sid:
            # No session header → can't apply session grain; the role check
            # above is the available authoritative gate. Mirrors the
            # legacy CLI behaviour (= header is opt-in).
            return
        if not trek_mod.is_session_id_keyed(t):
            # Pre-A trek: members[] is keyed by user_id only, so the
            # leader_session_id field may be stale or absent. Don't fail
            # the call — pre-A invariance is contractually preserved.
            return
        leader_sid = t.get("leader_session_id") or ""
        if not leader_sid:
            # Phase A+ trek with no live leader session (= e.g. just
            # archived or stamped null) — the role check above is the
            # available gate; refusing here would prevent ``take-over``
            # from binding a fresh session post-leader-death.
            return
        if caller_sid != leader_sid:
            raise HTTPException(
                status_code=403,
                detail=(
                    "leader action requires the stamped leader session "
                    f"(caller={caller_sid[:8]}.., leader={leader_sid[:8]}..)"
                ),
            )

    def _require_trek_joined_member(
        t: dict, user: dict, request: "Request | None" = None,
    ) -> None:
        """Raise 403 if caller is not a joined member (= invited but not joined
        is insufficient for write ops; mirrors SPEC 設計方針 12 join-flow).

        ms-97 / e-2658 Phase 1 — phase A+ trek の時は session_id grain 優先。
        placeholder (= session_id 未設定 + joined_at 空) は invited-but-not-joined
        として常に reject される。
        """
        if not is_auth_enabled():
            return
        uid = user.get("sub", "")
        sid = ""
        if request is not None:
            sid = request.headers.get("X-Beacon-Session", "") or ""
        member = _trek_find_member_dual(t, user_id=uid, session_id=sid)
        if member is not None and member.get("joined_at"):
            return
        raise HTTPException(status_code=403, detail="Only joined members can perform this action")

    def _list_related_treks(project_id: str, *, milestone: str = "",
                            operation: str = "", task: str = "",
                            user: dict | None) -> list:
        """Return treks visible to ``user`` whose scope matches this work item.

        Match rule: an entry counts if it is in the same project AND either
        (a) narrows to the exact ref, or (b) has no narrowing key (= covers
        the whole project, so the item is implicitly in scope).
        """
        actor = user.get("sub") if (is_auth_enabled() and user) else None
        candidates = db.list_treks(
            actor_id=actor,
            include_archived=True,  # widget renders historic associations too
        )
        out = []
        for t in candidates:
            for entry in t.get("scope") or []:
                if entry.get("project") != project_id:
                    continue
                has_narrow = bool(
                    entry.get("milestone")
                    or entry.get("operation")
                    or entry.get("task")
                )
                if not has_narrow:
                    out.append(t)
                    break
                if milestone and entry.get("milestone") == milestone:
                    out.append(t)
                    break
                if operation and entry.get("operation") == operation:
                    out.append(t)
                    break
                if task and entry.get("task") == task:
                    out.append(t)
                    break
        return out

    def _log_trek_scope_audit(*, action: str, trek_id: str, user: dict,
                              request: Request, entry: dict) -> None:
        """Emit a structured audit line for trek scope add/remove.

        Format mirrors the JSON-line shape Cloud Logging already parses from
        other server modules. Failure to log is silently swallowed — audit is
        observational, not load-bearing.
        """
        try:
            import json as _json
            sid = ""
            try:
                sid = request.headers.get("X-Beacon-Session", "") or ""
            except Exception:
                sid = ""
            uid = ""
            if isinstance(user, dict):
                uid = user.get("sub") or user.get("email") or ""
            record = {
                "evt": "trek.scope.audit",
                "action": action,  # "add" or "remove"
                "trek_id": trek_id,
                "user_id": uid,
                "session_id": sid,
                "entry": entry,
            }
            print(_json.dumps(record, ensure_ascii=False), flush=True)
        except Exception:
            pass

    def _record_halt_decision(project_id: str, trek_id: str, *, resumed: bool,
                              issuer_session_id: str, issuer_user_id: str,
                              context: str, rationale: str = "",
                              agent: str | None = None) -> None:
        """ms-90 / e-3247 + e-3241: Trek の halt / resume を decision-event に記録。

        記録失敗は halt / resume 自体を壊してはならない (= 付随的) ので握り潰して
        ログするだけ。project_id が空 (= home project 解決失敗) なら skip する。
        ``agent`` は who.agent (= 誰が halt/resume したか、e-6012) — 呼び出し側が
        認証 claims から ``agent_from_claims`` で解決して渡す。
        """
        if not project_id:
            return
        try:
            db.append_decision_event(
                project_id,
                decision_event_mod.decision_event_from_halt(
                    resumed=resumed,
                    trek_id=trek_id,
                    issuer_session_id=issuer_session_id,
                    issuer_user_id=issuer_user_id,
                    context=context,
                    rationale=(rationale or None),
                    agent=agent,
                ),
            )
        except Exception as _dec_exc:  # pragma: no cover - defensive
            logging.getLogger(__name__).warning(
                "append_decision_event (halt/resume) failed for trek_id=%s: %s",
                trek_id, _dec_exc,
            )

    def _trek_scope_by_project(t: dict) -> dict[str, list[dict]]:
        """Group ``t['scope']`` entries by their ``project`` field.

        Returns ``{project_id: [scope_entry, ...]}``. Entries missing the
        ``project`` field are silently dropped — they violate
        ``normalize_scope_entry`` and shouldn't exist on disk, but old data
        could; we skip rather than 500.
        """
        by_project: dict[str, list[dict]] = {}
        for entry in t.get("scope") or []:
            pid = entry.get("project") or ""
            if not pid:
                continue
            by_project.setdefault(pid, []).append(entry)
        return by_project

    def _scope_contributes(entries: list[dict], *, kind: str) -> tuple[bool, set[str]]:
        """Decide what a project's scope entries contribute for one entity kind.

        ``kind`` is ``"milestone"`` / ``"operation"`` / ``"task"``. Returns
        ``(include_all, narrow_ids)``:

        * ``include_all=True`` when any scope entry is project-wide (= no
          narrowing key) — the whole project's entities of this kind belong.
        * ``include_all=False, narrow_ids={...}`` when every matching entry
          narrows; ``narrow_ids`` is the union of the matching IDs.

        For ``kind="task"`` the contribution also widens via milestone /
        operation narrowing (= "all tasks under that MS / Op"). That widening
        is handled by the caller; this helper only reports direct task IDs.
        """
        if not entries:
            return False, set()
        narrow_ids: set[str] = set()
        for entry in entries:
            ms = entry.get("milestone") or ""
            op = entry.get("operation") or ""
            task = entry.get("task") or ""
            if not (ms or op or task):
                # Project-wide entry — whole project is in.
                return True, set()
            if kind == "milestone" and ms:
                narrow_ids.add(ms)
            elif kind == "operation" and op:
                narrow_ids.add(op)
            elif kind == "task" and task:
                narrow_ids.add(task)
        return False, narrow_ids

    def _sid_to_uid(project_id: str, session_id: str) -> str:
        """Resolve a session_id → user_id from a project's session registry.

        Single-session variant of ``_resolve_bus_event_user_ids`` (same
        ``projects/{pid}/sessions/{sid}.user_id`` source). Empty on any miss.
        """
        if not project_id or not session_id:
            return ""
        try:
            for s in db.list_sessions(project_id) or []:
                if (s.get("session_id") or "") == session_id:
                    return str(s.get("user_id") or "")
        except Exception:
            return ""
        return ""

    # ---- route handlers (verbatim bodies; @app -> @router) ----

    @router.get("/api/projects/{project_id}/milestones/{ms_id}/related-treks")
    def related_treks_for_milestone(project_id: str, ms_id: str,
                                    user: dict = Depends(require_auth)):
        """List treks visible to the caller whose scope covers this milestone.

        Includes archived treks (= the widget renders history). Returns the
        full trek doc per match; the Web UI picks status / title / archived_at
        for the badge rendering.
        """
        _load(project_id, user)  # project-side access check
        return _list_related_treks(project_id, milestone=ms_id, user=user)

    @router.get("/api/projects/{project_id}/operations/{op_id}/related-treks")
    def related_treks_for_operation(project_id: str, op_id: str,
                                    user: dict = Depends(require_auth)):
        """List treks whose scope covers this operation (e-1663 / e-1664)."""
        _load(project_id, user)
        return _list_related_treks(project_id, operation=op_id, user=user)

    @router.get("/api/projects/{project_id}/entries/{entry_id}/related-treks")
    def related_treks_for_entry(project_id: str, entry_id: str,
                                user: dict = Depends(require_auth)):
        """List treks whose scope covers this task entry (e-1663 / e-1664)."""
        _load(project_id, user)
        return _list_related_treks(project_id, task=entry_id, user=user)

    @router.get("/api/treks")
    def list_treks_endpoint(
        status: Optional[str] = None,
        include_archived: bool = False,
        all_actors: bool = False,
        user: dict = Depends(require_auth),
    ):
        """List treks visible to the caller.

        Default: treks where caller is creator OR member.
        ``?all_actors=true`` returns every trek (admin only; non-admin sees 403).
        ``?status=`` narrows to a specific lifecycle state.
        ``?include_archived=true`` includes archived treks (default hides them).
        """
        if all_actors:
            _require_admin(user)
            actor_filter = None
        else:
            actor_filter = user.get("sub") if is_auth_enabled() else None
        return db.list_treks(
            actor_id=actor_filter,
            status=status,
            include_archived=include_archived,
        )

    @router.post("/api/treks")
    def create_trek_endpoint(body: TrekCreate, user: dict = Depends(require_auth)):
        """Create a new trek. Caller becomes the creator + initial leader member.

        ``creator_session_id`` is recorded as ``leader_session_id`` (SPEC 設計方針 9).
        """
        if not body.creator_session_id:
            raise HTTPException(status_code=400, detail="creator_session_id required")
        try:
            new_doc = trek_mod.new_trek(
                title=body.title,
                creator_user_id=user.get("sub", ""),
                creator_email=user.get("email", ""),
                creator_session_id=body.creator_session_id,
                description=body.description,
                type_=body.type,
                cadence_minutes=body.cadence_minutes,
                manager_agent_url=body.manager_agent_url or "",
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        db.save_trek(new_doc["trek_id"], new_doc)
        return new_doc

    @router.get("/api/treks/{trek_id}")
    def get_trek_endpoint(trek_id: str, user: dict = Depends(require_auth)):
        """Get a single trek by id. Caller must be creator or member."""
        return _load_trek_for_read(trek_id, user)

    @router.patch("/api/treks/{trek_id}")
    def update_trek_endpoint(trek_id: str, body: TrekUpdate, request: Request,
                             user: dict = Depends(require_auth)):
        """Update title / description / type. Leader-only.

        Status / members / scope / halt are mutated through dedicated endpoints
        so audit logs and authz rules stay sharp per intent.

        ms-97 Phase 4 / AC13 — leader hard-check via
        ``_require_trek_leader_session`` (= role + session_id grain on phase
        A+ trek). Pre-A invariance preserved.
        """
        t = _load_trek_for_read(trek_id, user)
        _require_trek_leader_session(t, user, request)
        if body.title is not None:
            title = body.title.strip()
            if not title:
                raise HTTPException(status_code=400, detail="title cannot be empty")
            t["title"] = title
        if body.description is not None:
            t["description"] = body.description
        if body.type is not None:
            try:
                trek_mod.validate_type(body.type)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            t["type"] = body.type
        # ms-83 / e-1994: cadence_minutes / manager_agent_url live on ``meta``;
        # use the dedicated setters so validation + idempotency rules stay in
        # one place (= lib/trek.py). ``cadence_minutes is None`` here means
        # "field not supplied in this PATCH"; explicit clear uses a future
        # dedicated endpoint or 0 sentinel — kept out of this MS scope.
        if body.cadence_minutes is not None:
            try:
                trek_mod.set_cadence_minutes(
                    t, cadence_minutes=body.cadence_minutes
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
        if body.manager_agent_url is not None:
            trek_mod.set_manager_agent_url(
                t, manager_agent_url=body.manager_agent_url
            )
        t["updated_at"] = trek_mod.utcnow_iso()
        db.save_trek(trek_id, t)
        return t

    @router.delete("/api/treks/{trek_id}")
    def archive_trek_endpoint(trek_id: str, request: Request,
                              user: dict = Depends(require_auth)):
        """Archive a trek (status → archived). Leader-only. Archive is terminal.

        ms-97 Phase 4 / AC13 — leader hard-check via
        ``_require_trek_leader_session`` (= role + session_id grain on phase
        A+ trek). Pre-A invariance preserved.
        """
        t = _load_trek_for_read(trek_id, user)
        _require_trek_leader_session(t, user, request)
        cur = t.get("status", "")
        try:
            trek_mod.validate_transition(cur, "archived")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        now = trek_mod.utcnow_iso()
        t["status"] = "archived"
        t["archived_at"] = now
        t["updated_at"] = now
        db.save_trek(trek_id, t)
        return t

    @router.post("/api/treks/{trek_id}/start")
    def start_trek_endpoint(trek_id: str, request: Request,
                            user: dict = Depends(require_auth)):
        """Transition trek planning → active. Leader-only.

        ms-97 Phase 4 / AC13 — leader hard-check via
        ``_require_trek_leader_session`` (= role + session_id grain on phase
        A+ trek). Pre-A invariance preserved.
        """
        t = _load_trek_for_read(trek_id, user)
        _require_trek_leader_session(t, user, request)
        cur = t.get("status", "")
        try:
            trek_mod.validate_transition(cur, "active")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        t["status"] = "active"
        t["updated_at"] = trek_mod.utcnow_iso()
        db.save_trek(trek_id, t)
        return t

    @router.post("/api/treks/{trek_id}/members")
    def invite_trek_member_endpoint(trek_id: str, body: TrekInvite,
                                    user: dict = Depends(require_auth)):
        """Invite a user to the trek by email. Any joined member can invite.

        Invitee must already exist as a Beacon user (= signed in once). The
        invitation appears in members[] with ``joined_at=""`` until they call
        POST /api/treks/{id}/members/join.
        """
        t = _load_trek_for_read(trek_id, user)
        _require_trek_joined_member(t, user)
        found = db.find_user_by_email(body.email)
        if found is None:
            raise HTTPException(
                status_code=404,
                detail=f"User '{body.email}' not found. They must sign in to Beacon first.",
            )
        invited_id, _ = found
        try:
            trek_mod.add_invitation(
                t,
                user_id=invited_id,
                email=body.email,
                invited_by_user_id=user.get("sub", ""),
            )
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        db.save_trek(trek_id, t)
        return t

    @router.post("/api/treks/{trek_id}/members/join")
    def join_trek_endpoint(trek_id: str, request: Request,
                           user: dict = Depends(require_auth)):
        """Accept the caller's own invitation (= sets ``joined_at`` to now).

        Caller must already appear in members[] (= owner-issued invitation).
        Non-invited callers get 403 — there is no self-add path by design.

        We bypass _load_trek_for_read's membership check here because the trek
        visibility model is "creator OR member" and an invited-but-not-yet-joined
        user IS a member entry; the visibility check passes. Self-add (= caller
        not yet in members[] at all) gets caught by trek_mod.accept_invitation.

        ms-86 / e-2225 — also writes a session_history entry on the trek doc
        so the Trek keeps a cumulative record of every session that has ever
        joined. The session id is taken from the ``X-Beacon-Session`` header
        (= the same channel the task-state endpoint uses). When the header is
        missing the join still succeeds; the session_history entry is just
        skipped for that call.
        """
        t = _load_trek_for_read(trek_id, user)
        caller_sid = request.headers.get("X-Beacon-Session", "") or ""
        user_id = user.get("sub", "")
        try:
            trek_mod.accept_invitation(
                t, user_id=user_id, session_id=caller_sid,
            )
        except ValueError as e:
            # Not invited (no row at all) → 403, not 404. The trek exists; the
            # caller just cannot self-add.
            raise HTTPException(status_code=403, detail=str(e))
        # ms-97 / e-2637 — welcome tick bootstrap. After a successful join, if
        # this session has not yet received a welcome tick, fire one into its
        # home project bus so the fresh joiner (= claim ゼロ, AC33 lazy start
        # 不該当) gets a wake-up event that primes the kickoff ritual via
        # /beacon-trek-execute Skill. Idempotent: ``meta.welcome_tick_fired_at``
        # tracks per-session_id stamps so retries / re-joins do not re-fire.
        # The stamp + the bus event write share the same save_trek transaction
        # below so the audit trail is consistent.
        welcome_event_id = ""
        if caller_sid and trek_mod.should_fire_welcome_tick(
            t, session_id=caller_sid,
        ):
            welcome_event_id = _fire_welcome_tick(
                trek_doc=t, trek_id=trek_id, session_id=caller_sid,
            )
            if welcome_event_id:
                trek_mod.mark_welcome_tick_fired(t, session_id=caller_sid)
        db.save_trek(trek_id, t)
        # ms-97 / Phase 6 (AC15) — surface the accident-time leader candidate
        # notice on the join response so clients (= CLI / Skill / future UI) can
        # display the pre-notice to the invitee without re-deriving the text. The
        # member entry now carries ``meta.leader_candidate_notice_shown_at`` as
        # the audit stamp (= trek_mod.accept_invitation does that write).
        response = dict(t)
        response["leader_candidate_notice"] = (
            trek_mod.build_leader_candidate_notice(t)
        )
        if welcome_event_id:
            response["_welcome_tick_event_id"] = welcome_event_id
        return response

    @router.delete("/api/treks/{trek_id}/members/me")
    def leave_trek_endpoint(trek_id: str, request: Request,
                            user: dict = Depends(require_auth)):
        """Caller removes themselves from the trek.

        The leader must transfer leadership first (`POST .../transfer-leader`),
        and the last member cannot leave (= archive the trek instead).

        ms-97 / e-2658 Phase 1 (AC6) — phase A+ trek (= members[] が session_id
        keyed) で ``X-Beacon-Session`` header があれば、 該当 1 session の
        member entry だけを削除する (= 同 user の他 session entries は残る、
        session-grain leave)。 header 不在 / pre-A trek は従来の user-grain
        leave (= 同 user の全 entries を削除)。
        """
        t = _load_trek_for_read(trek_id, user, request)
        user_id = user.get("sub", "")
        caller_sid = request.headers.get("X-Beacon-Session", "") or ""
        try:
            if caller_sid and trek_mod.is_session_id_keyed(t):
                # session-grain leave (= 自分の 1 session のみ抜ける)。
                # session 不在なら user-grain にフォールバック (= placeholder
                # session_id 未設定 entry の挙動を保つ)。
                target = trek_mod.find_member(t, session_id=caller_sid)
                if target is not None:
                    trek_mod.remove_member(t, session_id=caller_sid)
                else:
                    trek_mod.remove_member(t, user_id=user_id)
            else:
                trek_mod.remove_member(t, user_id=user_id)
        except ValueError as e:
            # leader-still-leader / not-a-member / last-member → 400
            raise HTTPException(status_code=400, detail=str(e))
        db.save_trek(trek_id, t)
        return t

    @router.put("/api/treks/{trek_id}/scope")
    def add_trek_scope_endpoint(trek_id: str, body: TrekScopeOp,
                                request: Request,
                                user: dict = Depends(require_auth)):
        """Stage a scope-entry add for user approval (ms-97 / e-2626, AC23).

        Pre-e-2626 this was an immediate mutation (= ``scope[]`` grew before
        any explicit user OK). With AC23 the scope-add must mirror the
        scope-remove approval flow (= ``pending_user_approval`` state on
        ``trek_doc.pending_scope_ops[]``). The actual ``scope[]`` mutation
        lands only when the user runs ``beacon trek scope-approve
        <pending_id>`` (= POST ``/api/treks/{trek_id}/scope/approve/
        {pending_id}``).

        The legacy audit log (ms-95 / e-2320) still emits, but now with
        ``action="add_pending"`` so the Cloud Logging filter can tell the
        request stage (``pending``) from the apply stage
        (``scope_add_approved``).

        AC24 (= blanket pre-approval, e-2603) is layered on top in a
        follow-up task. This endpoint stages unconditionally; the
        auto-commit on blanket-approved categories lands separately.
        """
        t = _load_trek_for_read(trek_id, user)
        _require_trek_joined_member(t, user)
        # ms-97 / e-2694 dogfood fix: expand short project names (=
        # ``life-plan-simulator``) to canonical full ids (=
        # ``life-plan-simulator-68c5df``) BEFORE we persist the scope entry.
        # Without this, scope rows storing the short name silently mismatch
        # against the registry-keyed session lookups (= ``list_sessions`` and
        # DM fanout key off the full id), so members in those projects
        # disappear from the fanout target list.
        requesting_user_id = (user.get("sub") or "").strip()
        canonical_pid = body.project
        if requesting_user_id:
            resolved = _resolve_canonical_project_id(
                body.project, user_id=requesting_user_id,
            )
            if resolved:
                canonical_pid = resolved
        entry: dict = {"project": canonical_pid}
        # ms-109 e-3699: copy whichever narrowing key is present, occupation-
        # agnostic (dev milestone/operation/task + sales opportunity/account).
        for _k in trek_mod.NARROWING_KEYS:
            _v = getattr(body, _k, None)
            if _v:
                entry[_k] = _v
        # ms-97 / e-2659 (AC7 server layer): explicit strict-mode validation
        # so the project-wide rejection surfaces as HTTPException 400 with the
        # user-facing message, distinct from the 409 "already present" path
        # below. add_pending_scope_op will also run strict mode internally,
        # this front-load is for the clean status code split.
        try:
            entry = trek_mod.normalize_scope_entry(entry, strict=True)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        # Resolve the requesting session id from the X-Beacon-Session header so
        # the pending record carries the "who asked" attribution. Falls back to
        # empty string when the header is absent (= same null behaviour as the
        # audit helper, callers without a session header still get to stage).
        sid = ""
        try:
            sid = request.headers.get("X-Beacon-Session", "") or ""
        except Exception:
            sid = ""
        # ms-97 / Phase 7-C / AC24 — blanket pre-approval auto-commit. When the
        # leader has registered a matching category (= e.g. "operation" or
        # "milestone:ms-97"), bypass ``add_pending_scope_op`` and apply the
        # add directly via ``add_scope_entry``. This is the "no DM hop" path.
        if trek_mod.is_blanket_approved(t, entry):
            try:
                trek_mod.add_scope_entry(t, entry=entry)
            except ValueError as e:
                raise HTTPException(status_code=409, detail=str(e))
            db.save_trek(trek_id, t)
            _log_trek_scope_audit(
                action="add_blanket_auto_commit", trek_id=trek_id, user=user,
                request=request, entry=entry,
            )
            out = dict(t)
            out["pending_op"] = None
            out["auto_committed"] = True
            out["committed_via"] = "blanket_approval"
            return out
        try:
            rec = trek_mod.add_pending_scope_op(
                t,
                action=trek_mod.PENDING_SCOPE_ACTION_ADD,
                entry=entry,
                requested_by_session_id=sid,
            )
        except ValueError as e:
            # Normalisation failure (= bad entry shape). 409 to match the
            # legacy contract on the immediate path.
            raise HTTPException(status_code=409, detail=str(e))
        db.save_trek(trek_id, t)
        _log_trek_scope_audit(
            action="add_pending", trek_id=trek_id, user=user,
            request=request, entry=entry,
        )
        # Return the trek doc unchanged in shape, plus the pending record so
        # callers can immediately reference the ``pending_id`` for approve /
        # reject. Existing clients that ignored the body keep working.
        out = dict(t)
        out["pending_op"] = rec
        out["auto_committed"] = False
        return out

    @router.delete("/api/treks/{trek_id}/scope")
    def remove_trek_scope_endpoint(trek_id: str, body: TrekScopeOp,
                                   request: Request,
                                   user: dict = Depends(require_auth)):
        """Stage a scope-entry removal for user approval (ms-97 / e-2611, AC25).

        Pre-e-2611 this was an immediate mutation. With AC25 the scope-remove
        must mirror the scope-add approval flow (= ``pending_user_approval``
        state on ``trek_doc.pending_scope_ops[]``). The actual ``scope[]``
        mutation lands only when the user runs
        ``beacon trek scope-approve <pending_id>`` (= POST
        ``/api/treks/{trek_id}/scope/approve/{pending_id}``).

        The legacy audit log (ms-95 / e-2320) still emits, but now with
        ``action="remove_pending"`` so the Cloud Logging filter can tell the
        request stage (``pending``) from the apply stage (``remove_approved``).
        """
        t = _load_trek_for_read(trek_id, user)
        _require_trek_joined_member(t, user)
        entry: dict = {"project": body.project}
        if body.milestone:
            entry["milestone"] = body.milestone
        if body.operation:
            entry["operation"] = body.operation
        if body.task:
            entry["task"] = body.task
        # Resolve the requesting session id from the X-Beacon-Session header so
        # the pending record carries the "who asked" attribution. Falls back to
        # empty string when the header is absent (= same null behaviour as the
        # audit helper, callers without a session header still get to stage).
        sid = ""
        try:
            sid = request.headers.get("X-Beacon-Session", "") or ""
        except Exception:
            sid = ""
        try:
            rec = trek_mod.add_pending_scope_op(
                t,
                action=trek_mod.PENDING_SCOPE_ACTION_REMOVE,
                entry=entry,
                requested_by_session_id=sid,
            )
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        db.save_trek(trek_id, t)
        _log_trek_scope_audit(
            action="remove_pending", trek_id=trek_id, user=user,
            request=request, entry=entry,
        )
        # Return the trek doc unchanged in shape, plus the pending record so
        # callers can immediately reference the ``pending_id`` for approve /
        # reject. Existing clients that ignored the body keep working.
        out = dict(t)
        out["pending_op"] = rec
        return out

    @router.post("/api/treks/{trek_id}/scope/approve/{pending_id}")
    def approve_trek_scope_op_endpoint(trek_id: str, pending_id: str,
                                       request: Request,
                                       user: dict = Depends(require_auth)):
        """Approve a pending scope op (= commit add or remove).

        ms-97 / e-2611 — Mirror of the scope-remove staging endpoint. Any
        joined member of the trek may approve; the philosophy is that
        ``pending_user_approval`` is a structural pause for an explicit
        "yes do that" rather than a per-actor authorisation check. The same
        ``trek.scope.audit`` log line is emitted with
        ``action="<scope_add|remove>_approved"`` so the apply step is
        observable in Cloud Logging alongside the original request stage.
        """
        t = _load_trek_for_read(trek_id, user)
        _require_trek_joined_member(t, user)
        rec = trek_mod.find_pending_scope_op(t, pending_id=pending_id)
        if rec is None:
            raise HTTPException(
                status_code=404,
                detail=f"pending scope op not found: {pending_id}",
            )
        try:
            applied_entry = trek_mod.approve_pending_scope_op(
                t, pending_id=pending_id,
            )
        except ValueError as e:
            # The drop happened before raise (see lib helper), so save the
            # now-cleaned doc and 409 the caller. This matches the
            # add_scope_entry / remove_scope_entry 409 contract on add.
            db.save_trek(trek_id, t)
            raise HTTPException(status_code=409, detail=str(e))
        db.save_trek(trek_id, t)
        audit_action = f"{rec.get('action', 'scope_op')}_approved"
        _log_trek_scope_audit(
            action=audit_action, trek_id=trek_id, user=user,
            request=request, entry=applied_entry,
        )
        return t

    @router.post("/api/treks/{trek_id}/scope/reject/{pending_id}")
    def reject_trek_scope_op_endpoint(trek_id: str, pending_id: str,
                                      request: Request,
                                      user: dict = Depends(require_auth)):
        """Reject a pending scope op (= drop without applying).

        ms-97 / e-2611 — Companion to ``approve``. Any joined member may
        reject. Audit line carries
        ``action="<scope_add|remove>_rejected"`` so the rejection is just as
        traceable as approve / apply.
        """
        t = _load_trek_for_read(trek_id, user)
        _require_trek_joined_member(t, user)
        rec = trek_mod.find_pending_scope_op(t, pending_id=pending_id)
        if rec is None:
            raise HTTPException(
                status_code=404,
                detail=f"pending scope op not found: {pending_id}",
            )
        try:
            dropped = trek_mod.reject_pending_scope_op(
                t, pending_id=pending_id,
            )
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        db.save_trek(trek_id, t)
        audit_action = f"{dropped.get('action', 'scope_op')}_rejected"
        _log_trek_scope_audit(
            action=audit_action, trek_id=trek_id, user=user,
            request=request, entry=dropped.get("entry") or {},
        )
        return t

    @router.post("/api/treks/{trek_id}/slots")
    def add_trek_slot_endpoint(trek_id: str, body: TrekSlotAdd,
                               request: Request,
                               user: dict = Depends(require_auth)):
        """Stage a slot-add pending op with a fresh ``sl-<8 hex>`` id.

        ms-99 / e-2830 (SPEC 方針 6 + AC 14): shape validation runs
        server-side so malformed calls surface HTTP 400 rather than 409
        "already present". Successful staging returns the trek doc plus a
        ``pending_op`` record — same envelope shape as the scope-add
        endpoint so cross-verb clients can read the id uniformly.
        """
        t = _load_trek_for_read(trek_id, user)
        _require_trek_joined_member(t, user)
        # Resolve short project names (= "life-plan-simulator") to canonical
        # suffixed ids at the boundary, matching add_trek_scope_endpoint.
        requesting_user_id = (user.get("sub") or "").strip()
        canonical_pid = body.project
        if requesting_user_id:
            resolved = _resolve_canonical_project_id(
                body.project, user_id=requesting_user_id,
            )
            if resolved:
                canonical_pid = resolved
        entry: dict = {"project": canonical_pid}
        # ms-109 e-3699: occupation-agnostic narrowing (dev milestone/operation/
        # task + sales opportunity/account), sourced from trek_mod.NARROWING_KEYS.
        for _k in trek_mod.NARROWING_KEYS:
            _v = getattr(body, _k, None)
            if _v:
                entry[_k] = _v
        # Reject empty narrowing at 400 (= same policy as add_trek_scope but
        # with the v2 slot verb error message). ``included_task_ids`` on a
        # non-milestone slot is meaningless — refuse politely below.
        narrowing = [k for k in trek_mod.NARROWING_KEYS if entry.get(k)]
        _kinds = " | ".join(trek_mod.NARROWING_KEYS)
        if len(narrowing) == 0:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"slot add requires one narrowing key: {_kinds} "
                    "(= slot must narrow the project, ms-97 AC7)"
                ),
            )
        if len(narrowing) > 1:
            raise HTTPException(
                status_code=400,
                detail=f"slot add accepts exactly one of {_kinds}",
            )
        if body.included_task_ids is not None:
            if "milestone" not in entry:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "included_task_ids is only valid on milestone slots "
                        "(SPEC 方針 2: MS slot child opt-in)"
                    ),
                )
            entry["included_task_ids"] = list(body.included_task_ids)
        sid = ""
        try:
            sid = request.headers.get("X-Beacon-Session", "") or ""
        except Exception:
            sid = ""
        try:
            rec = trek_mod.add_pending_scope_op(
                t,
                action=trek_mod.PENDING_SCOPE_ACTION_ADD,
                entry=entry,
                requested_by_session_id=sid,
                mint_slot=True,
            )
        except ValueError as e:
            # normalize_scope_entry ValueError = shape violation → 400
            msg = str(e)
            if "already present" in msg or "already exists" in msg:
                raise HTTPException(status_code=409, detail=msg)
            raise HTTPException(status_code=400, detail=msg)
        db.save_trek(trek_id, t)
        _log_trek_scope_audit(
            action="slot_add_pending", trek_id=trek_id, user=user,
            request=request, entry=entry,
        )
        out = dict(t)
        out["pending_op"] = rec
        return out

    @router.patch("/api/treks/{trek_id}/slots/{slot_id}")
    def amend_trek_slot_endpoint(trek_id: str, slot_id: str,
                                 body: TrekSlotAmend, request: Request,
                                 user: dict = Depends(require_auth)):
        """Stage a slot-amend pending op (edit ``included_task_ids``).

        ms-99 / e-2830: 404 when the slot doesn't exist, 400 when the
        request would be a no-op (= neither add_children nor
        remove_children given). Applying the amend materialises legacy
        null semantics to an explicit list at approve time (SPEC AC 4).
        """
        t = _load_trek_for_read(trek_id, user)
        _require_trek_joined_member(t, user)
        if not body.add_children and not body.remove_children:
            raise HTTPException(
                status_code=400,
                detail=(
                    "slot amend requires at least one add_children or "
                    "remove_children entry"
                ),
            )
        sid = ""
        try:
            sid = request.headers.get("X-Beacon-Session", "") or ""
        except Exception:
            sid = ""
        try:
            rec = trek_mod.add_pending_slot_amend_op(
                t,
                slot_id=slot_id,
                add_children=list(body.add_children),
                remove_children=list(body.remove_children),
                requested_by_session_id=sid,
            )
        except ValueError as e:
            msg = str(e)
            if "slot not found" in msg:
                raise HTTPException(status_code=404, detail=msg)
            raise HTTPException(status_code=400, detail=msg)
        db.save_trek(trek_id, t)
        _log_trek_scope_audit(
            action="slot_amend_pending", trek_id=trek_id, user=user,
            request=request, entry={"slot_id": slot_id,
                                    "add": rec.get("add_children") or [],
                                    "remove": rec.get("remove_children") or []},
        )
        out = dict(t)
        out["pending_op"] = rec
        return out

    @router.post("/api/treks/{trek_id}/slots/{slot_id}/claim")
    def claim_trek_slot_endpoint(trek_id: str, slot_id: str,
                                 body: TrekSlotClaim, request: Request,
                                 user: dict = Depends(require_auth)):
        """Stage a slot-claim pending op (stamp ``claim_session_id``).

        ms-99 / e-2830 (SPEC 方針 4): claim never changes task state.
        ``session_id=""`` is the unclaim gesture — it clears the fields
        when approved. 404 when the slot doesn't exist.
        """
        t = _load_trek_for_read(trek_id, user)
        _require_trek_joined_member(t, user)
        requested_by = ""
        try:
            requested_by = request.headers.get("X-Beacon-Session", "") or ""
        except Exception:
            requested_by = ""
        try:
            rec = trek_mod.add_pending_slot_claim_op(
                t,
                slot_id=slot_id,
                session_id=body.session_id or "",
                requested_by_session_id=requested_by,
            )
        except ValueError as e:
            msg = str(e)
            if "slot not found" in msg:
                raise HTTPException(status_code=404, detail=msg)
            raise HTTPException(status_code=400, detail=msg)
        db.save_trek(trek_id, t)
        verb = "unclaim" if not body.session_id else "claim"
        _log_trek_scope_audit(
            action=f"slot_{verb}_pending", trek_id=trek_id, user=user,
            request=request, entry={"slot_id": slot_id,
                                    "session_id": body.session_id or ""},
        )
        out = dict(t)
        out["pending_op"] = rec
        return out

    @router.get("/api/treks/{trek_id}/slots")
    def list_trek_slots_endpoint(trek_id: str,
                                 user: dict = Depends(require_auth)):
        """Return the materialize slot view of the trek (Phase 1 projection).

        ms-99 / e-2830: any authed viewer can list; membership isn't
        required for read since ``GET /api/treks/{id}`` (the parent trek
        doc) already permits the same audience.
        """
        t = _load_trek_for_read(trek_id, user)
        rows = trek_mod.materialize_slot_view(t)
        return {"trek_id": trek_id, "slots": rows}

    @router.put("/api/treks/{trek_id}/halt")
    def set_trek_halt_endpoint(trek_id: str, body: TrekHaltSet,
                               user: dict = Depends(require_auth)):
        """Pull the Andon cord. Any joined member may halt an active trek.

        Halt is metadata, not a status: trek stays ``active`` while halted.
        Sessions observe the halt field and pause autonomous work. Resume by
        DELETE on this same path.
        """
        t = _load_trek_for_read(trek_id, user)
        _require_trek_joined_member(t, user)
        if not body.issued_by_session_id:
            raise HTTPException(status_code=400, detail="issued_by_session_id required")
        try:
            trek_mod.set_halt(
                t,
                issued_by_session_id=body.issued_by_session_id,
                reason=body.reason or "",
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        db.save_trek(trek_id, t)
        # ms-90 / e-3247: halt (= 中断発令) を decision-event に記録する。理由 (=
        # 直面した問題) を context に置く。記録失敗は halt を壊さない (= 付随的)。
        _record_halt_decision(
            _resolve_leader_home_project_id(t), trek_id, resumed=False,
            issuer_session_id=body.issued_by_session_id,
            issuer_user_id=user.get("sub") or "", context=body.reason or "",
            rationale=body.rationale or "",
            agent=decision_event_mod.agent_from_claims(user),
        )
        return t

    @router.delete("/api/treks/{trek_id}/halt")
    def clear_trek_halt_endpoint(trek_id: str, user: dict = Depends(require_auth)):
        """Release the Andon cord. Any joined member."""
        t = _load_trek_for_read(trek_id, user)
        _require_trek_joined_member(t, user)
        trek_mod.clear_halt(t)
        db.save_trek(trek_id, t)
        # ms-90 / e-3247: resume (= 中断解除) を decision-event に記録する。
        _record_halt_decision(
            _resolve_leader_home_project_id(t), trek_id, resumed=True,
            issuer_session_id="", issuer_user_id=user.get("sub") or "", context="",
            agent=decision_event_mod.agent_from_claims(user),
        )
        return t

    @router.post("/api/treks/{trek_id}/summary-sent")
    def trek_summary_sent_endpoint(
        trek_id: str, request: Request,
        user: dict = Depends(require_auth),
    ):
        """Stamp ``meta.summary_sent_at`` after the leader sent the user
        summary DM (ms-97 / Phase 7-A / AC21).

        Leader-only via ``_require_trek_leader_session`` (= role + session_id
        grain on phase A+ trek、 pre-A invariance preserved)。 stamp 後は
        leader-digest tick が停止条件に入る (= completion_notified_at と
        summary_sent_at の両方 stamp で leader-digest fire を skip する、
        AC21)。

        Idempotent: re-calling stamps the latest timestamp but the leader-tick
        stop condition only cares about "non-null". Returns the updated trek
        doc so callers can echo ``meta.summary_sent_at`` back to the user.
        """
        t = _load_trek_for_read(trek_id, user)
        _require_trek_leader_session(t, user, request)
        meta = t.setdefault("meta", {})
        meta["summary_sent_at"] = trek_mod.utcnow_iso()
        t["updated_at"] = trek_mod.utcnow_iso()
        db.save_trek(trek_id, t)
        return t

    @router.post("/api/treks/{trek_id}/migrate-members-session-keyed")
    def migrate_trek_members_session_keyed_endpoint(
        trek_id: str,
        request: Request,
        dry_run: bool = False,
        user: dict = Depends(require_auth),
    ):
        """Migrate trek.members[] from user_id keyed to session_id keyed (ms-97 / e-2658 AC6).

        Leader-only via ``_require_trek_leader_session``. Applies the pure
        mutator ``trek_mod.migrate_members_to_session_keyed`` and persists
        via ``db.save_trek``. Live Firestore trek (= tk-XXXXXXXX) を CLI から
        cloud-mode で書き換えるための endpoint。 local-mode は CLI 側で別経路
        (= scripts/migrate_members_session_keyed.py local fallback)。

        Idempotency:
            Returns 409 if the trek is already past pre-A phase (= refuse
            double migration to keep the inverse rollback unambiguous).

        Args:
            dry_run: ``?dry_run=true`` で migrate 後の trek_doc を返すが
                ``db.save_trek`` は呼ばない (= 事前確認用)。
        """
        t = _load_trek_for_read(trek_id, user)
        _require_trek_leader_session(t, user, request)
        try:
            trek_mod.migrate_members_to_session_keyed(t)
        except ValueError as exc:
            # Double-migration refusal (= already at phase A/B/C) returns 409
            # Conflict so the CLI / operator can distinguish "already done"
            # from a 4xx validation error.
            raise HTTPException(status_code=409, detail=str(exc))
        if not dry_run:
            db.save_trek(trek_id, t)
        return t

    @router.post("/api/treks/{trek_id}/transfer-leader")
    def transfer_trek_leader_endpoint(trek_id: str, body: TrekTransferLeader,
                                      user: dict = Depends(require_auth)):
        """Hand off leadership to another session.

        Two-factor check (session AND user grain):
          * ``from_session_id`` must equal the current ``leader_session_id`` —
            confirms the caller is the live leader session.
          * The calling user must hold the ``leader`` role in members[] —
            confirms identity at the user grain (= survives session restart).
        """
        t = _load_trek_for_read(trek_id, user)
        if not body.from_session_id or not body.to_session_id:
            raise HTTPException(status_code=400,
                                detail="from_session_id and to_session_id required")
        if t.get("leader_session_id") != body.from_session_id:
            raise HTTPException(
                status_code=403,
                detail="from_session_id does not match current trek leader",
            )
        _require_trek_leader(t, user)
        try:
            trek_mod.transfer_leader(t, target_session_id=body.to_session_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        db.save_trek(trek_id, t)
        return t

    @router.post("/api/treks/{trek_id}/take-over")
    def take_over_trek_endpoint(trek_id: str, body: TrekTakeOver,
                                user: dict = Depends(require_auth)):
        """Fresh-session leader take-over (ms-88 / e-2089).

        Unlike ``transfer-leader`` which requires the **live** prior leader
        session to authorize, take-over only checks the user-grain leader role
        — so a fresh bclaude session of the same user can recover when the
        original leader session is dead (= Mac restart, terminal closed,
        bclaude relaunched). This closes the dogfood Finding 1 silent-ack
        path: a dead ``leader_session_id`` was stale-but-non-null, scheduler
        fan-out kept aiming at it, and there was no way to re-bind without
        going through the dead session.

        Auth (user grain only, no from_session check):
          * Caller must be a joined member (= ``find_member`` non-null AND
            ``joined_at`` non-empty)
          * Caller must hold the ``leader`` role (= ``_require_trek_leader``)

        Idempotent: re-binding to the same ``session_id`` is a no-op
        (``trek_mod.transfer_leader`` already handles the equality case).
        """
        t = _load_trek_for_read(trek_id, user)
        if not body.session_id:
            raise HTTPException(status_code=400, detail="session_id required")
        _require_trek_leader(t, user)
        # joined check is implicit in _require_trek_leader (= role only set
        # after joining), but we re-affirm here so the error message is precise
        # if a future refactor splits role from joined_at.
        if is_auth_enabled():
            uid = user.get("sub") or ""
            member = trek_mod.find_member(t, user_id=uid)
            if not member or not member.get("joined_at"):
                raise HTTPException(
                    status_code=403,
                    detail="take-over requires a joined leader member",
                )
        try:
            trek_mod.transfer_leader(t, target_session_id=body.session_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        # ms-88 / e-2138 — take-over した fresh session は kickoff_pending=true に
        # 強制 reset。 前 session の kickoff が完了済でも、 新 session は別の plan
        # / worktree を持つ可能性があるので、 peer に再度 announce する義務を持つ。
        uid_for_reset = (user.get("sub") if is_auth_enabled() else "") or ""
        trek_mod.reset_kickoff_pending(
            t, session_id=body.session_id, user_id=uid_for_reset,
        )
        db.save_trek(trek_id, t)
        return t

    @router.post("/api/treks/{trek_id}/succession-consent")
    def trek_succession_consent_endpoint(
        trek_id: str,
        body: TrekSuccessionConsent,
        request: Request,
        user: dict = Depends(require_auth),
    ):
        """AC22 1 hop consent endpoint — candidate accepts / declines leadership.

        ms-97 / Phase 7-B / e-2684. Called by the candidate session that
        received a ``trek-leader-succession-consent`` DM. Strict caller gate:

          * Caller session_id (= ``X-Beacon-Session`` header) MUST equal
            ``meta.succession_pending_candidate`` (= the session the
            orchestrator nominated this cycle). Any other session is 403.
          * ``decision`` must be ``"accept"`` or ``"decline"``.

        On accept:
          * ``leader_session_id`` を caller_sid に transfer (= ``transfer_leader``)
          * ``session_history`` に role_at_join="leader" を upsert
          * ``meta.succession_pending_*`` / ``succession_declined`` /
            ``succession_escalated_at`` を全てクリア (= clear_succession_state)
          * caller の ``kickoff_status`` を pending=True に reset (= 新 leader
            は peer に再度 plan を宣言する義務、 ms-88 / e-2138 互換)

        On decline:
          * ``meta.succession_declined`` に caller_sid を追加 (= 次 tick で
            別 candidate を選ぶ)
          * ``meta.succession_pending_*`` をクリア
          * leader 役はそのまま (= 引き続き不応のままなら次 tick で再 nominate)
        """
        t = _load_trek_for_read(trek_id, user, request)
        decision = (body.decision or "").strip().lower()
        if decision not in ("accept", "decline"):
            raise HTTPException(
                status_code=400,
                detail="decision must be 'accept' or 'decline'",
            )
        caller_sid = ""
        if request is not None:
            caller_sid = request.headers.get("X-Beacon-Session", "") or ""
        if not caller_sid:
            raise HTTPException(
                status_code=400,
                detail="X-Beacon-Session header required for succession consent",
            )
        meta = t.get("meta") or {}
        pending_sid = (
            meta.get(trek_mod.SUCCESSION_PENDING_CANDIDATE_META_KEY) or ""
        )
        if not pending_sid:
            raise HTTPException(
                status_code=400,
                detail="no succession candidate pending for this trek",
            )
        if caller_sid != pending_sid:
            raise HTTPException(
                status_code=403,
                detail=(
                    "only the pending succession candidate can respond "
                    f"(caller={caller_sid[:8]}.., pending={pending_sid[:8]}..)"
                ),
            )
        member = trek_mod.find_member(t, session_id=caller_sid)
        if member is None:
            raise HTTPException(
                status_code=403,
                detail="caller is not a member of this trek",
            )
        if decision == "accept":
            try:
                trek_mod.transfer_leader(t, target_session_id=caller_sid)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            # Promote member role + record session_history at role_at_join=leader.
            member["role"] = "leader"
            trek_mod.upsert_session_history(
                t,
                session_id=caller_sid,
                user_id=member.get("user_id") or "",
                email=member.get("email") or "",
                role_at_join="leader",
            )
            # Fresh leader session must re-kickoff (= announce plan to peers).
            trek_mod.reset_kickoff_pending(
                t,
                session_id=caller_sid,
                user_id=member.get("user_id") or "",
            )
            trek_mod.clear_succession_state(t)
            db.save_trek(trek_id, t)
            return t
        # Decline path
        trek_mod.record_succession_decline(t, caller_sid)
        db.save_trek(trek_id, t)
        return t

    @router.post("/api/treks/{trek_id}/kickoff")
    def trek_kickoff_endpoint(trek_id: str, body: TrekKickoff,
                              user: dict = Depends(require_auth)):
        """Mark a session's kickoff DM as sent (ms-88 / e-2138).

        Called by ``/beacon-trek-pulse`` Skill's Step 0 after it has generated
        and sent the kickoff DM to the trek leader. Until this endpoint is
        called, ``pulse-ack`` rejects further progress for the same session
        with HTTP 400 ``kickoff_required``.

        Auth: caller must be a trek member at the user grain. The endpoint
        itself does not verify the DM was actually sent — that is a Skill /
        audit-side concern. The structural enforcement is "no pulse-ack
        progress until kickoff endpoint is called", which is sufficient to
        force the executor through the Skill body's Step 0.

        Returns the updated per-session kickoff_status entry so the Skill can
        echo "stamped" back to the user.
        """
        t = _load_trek_for_read(trek_id, user)
        _reject_if_trek_archived(t)
        if is_auth_enabled():
            uid = user.get("sub") or ""
            if not trek_mod.find_member(t, user_id=uid):
                raise HTTPException(
                    status_code=403,
                    detail="only trek members can mark kickoff completed",
                )
        if not body.session_id:
            raise HTTPException(status_code=400, detail="session_id required")
        try:
            uid_for_stamp = (user.get("sub") if is_auth_enabled() else "") or ""
            trek_mod.mark_kickoff_completed(
                t,
                session_id=body.session_id,
                user_id=uid_for_stamp,
                kickoff_dm_event_id=body.kickoff_dm_event_id or "",
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        db.save_trek(trek_id, t)
        return (t.get(trek_mod.KICKOFF_HISTORY_KEY) or {}).get(body.session_id) or {}

    @router.get("/api/treks/{trek_id}/kickoff")
    def trek_kickoff_status_endpoint(trek_id: str,
                                      user: dict = Depends(require_auth)):
        """Per-session kickoff summary (ms-88 / e-2138)."""
        t = _load_trek_for_read(trek_id, user)
        if is_auth_enabled():
            uid = user.get("sub") or ""
            if not trek_mod.find_member(t, user_id=uid):
                raise HTTPException(
                    status_code=403,
                    detail="only trek members can read kickoff status",
                )
        return trek_mod.summarize_kickoff_status(t)

    @router.post("/api/treks/{trek_id}/blanket-approve")
    def trek_blanket_approve_endpoint(trek_id: str, body: TrekBlanketCategory,
                                      request: Request,
                                      user: dict = Depends(require_auth)):
        """Register ``category`` as a blanket pre-approval (AC24).

        Subsequent scope-add requests whose entry matches the category will
        auto-commit (= bypass pending stage). Leader-only.
        """
        t = _load_trek_for_read(trek_id, user)
        _require_trek_leader_session(t, user, request)
        try:
            trek_mod.add_blanket_approval(t, body.category)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        db.save_trek(trek_id, t)
        return {
            "trek_id": trek_id,
            "blanket_scope_approvals": trek_mod.list_blanket_approvals(t),
        }

    @router.post("/api/treks/{trek_id}/blanket-revoke")
    def trek_blanket_revoke_endpoint(trek_id: str, body: TrekBlanketCategory,
                                     request: Request,
                                     user: dict = Depends(require_auth)):
        """Remove ``category`` from blanket pre-approvals (AC24). Leader-only."""
        t = _load_trek_for_read(trek_id, user)
        _require_trek_leader_session(t, user, request)
        try:
            trek_mod.remove_blanket_approval(t, body.category)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        db.save_trek(trek_id, t)
        return {
            "trek_id": trek_id,
            "blanket_scope_approvals": trek_mod.list_blanket_approvals(t),
        }

    @router.get("/api/treks/{trek_id}/logs.jsonl")
    def trek_logs_jsonl_endpoint(trek_id: str, request: Request,
                                 since: str = "",
                                 user: dict = Depends(require_auth)):
        """Stream the trek's structured logs as NDJSON (AC27).

        One JSON object per line, ascending by ``created_at``. RBAC: any
        joined member (= same surface as ``_load_trek_for_read``). Optional
        ``?since=<ISO8601>`` filters older rows so callers can resume.
        """
        t = _load_trek_for_read(trek_id, user, request)
        # Cap at a generous limit to keep streaming bounded; clients that
        # need everything can page via ``since``.
        rows = db.list_trek_logs(trek_id, limit=10000, since=since or "")
        # FastAPI StreamingResponse with NDJSON content type.
        from fastapi.responses import StreamingResponse
        import json as _json

        def _generator():
            for row in rows:
                try:
                    yield _json.dumps(row, ensure_ascii=False) + "\n"
                except Exception:
                    # Skip un-serialisable rows defensively (= one bad row
                    # must not break the stream).
                    continue
        # Make sure auth side-effects on ``t`` aren't accidentally serialised.
        del t  # noqa: F841
        return StreamingResponse(_generator(), media_type="application/x-ndjson")

    @router.post("/api/treks/{trek_id}/pulse-ack")
    def trek_pulse_ack_endpoint(trek_id: str, body: TrekPulseAck,
                                user: dict = Depends(require_auth)):
        """Record /beacon-trek-pulse Skill invocation (ms-88 / e-2106).

        Layer 2 (= observability) of the 3-layer trek autonomy harness
        (CORE doc 5nfTSmCDVUzD4SLzIhI5). The Skill calls this endpoint as its
        very first Step so the server has **ground truth** about whether the
        Skill actually fired in response to a scheduler tick. This closes the
        "Skill marker visible in executor terminal" verification hole that
        dogfood (= tk-40b0b27c) could not check directly.

        Auth: caller must be a trek member (= user grain, same as task-state).
        The Skill is invoked by the executor session, so the calling user is
        naturally a joined member.

        Returns the updated pulse_acks entry for the caller's session so the
        Skill can echo the recorded state back to the user.
        """
        t = _load_trek_for_read(trek_id, user)
        _reject_if_trek_archived(t)
        if is_auth_enabled():
            uid = user.get("sub") or ""
            if not trek_mod.find_member(t, user_id=uid):
                raise HTTPException(
                    status_code=403,
                    detail="only trek members can pulse-ack",
                )
        if not body.session_id:
            raise HTTPException(status_code=400, detail="session_id required")
        # ms-88 / e-2138 — Kickoff Ritual physical gate. 自セッションが kickoff DM を
        # 送信していなければ pulse-ack を拒否し、 Skill side の Step 0 (= kickoff DM
        # 自動生成 + leader 宛送信) を走らせる。 narrative ではなく server-side
        # validation で「kickoff 未送信なら progress 不可」 を物理的に閉じる。
        if trek_mod.get_kickoff_pending(t, session_id=body.session_id):
            raise HTTPException(
                status_code=400,
                detail=(
                    "kickoff_required: this session has not yet sent its kickoff DM. "
                    "Run /beacon-trek-pulse Step 0 to send the kickoff DM to the trek "
                    "leader (= self-info + plan + worktree + non-touch range), then "
                    "POST /api/treks/{trek_id}/kickoff to mark it completed, then "
                    "retry pulse-ack."
                ),
            )
        try:
            trek_mod.record_pulse_ack(
                t,
                session_id=body.session_id,
                picked_choice=body.picked_choice or "",
                note=body.note or "",
                # ms-92 / e-2165 — structured payload. Server passes them
                # through as-is; record_pulse_ack handles normalisation +
                # truncation. Pre-e-2165 bridges omit these (Pydantic
                # default-fills empty values), so behaviour is unchanged
                # for legacy callers.
                state_summary=body.state_summary or "",
                blockers=body.blockers or [],
                needs_leader_judgment=bool(body.needs_leader_judgment),
                time_on_task_seconds=int(body.time_on_task_seconds or 0),
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        db.save_trek(trek_id, t)
        # ms-97 / Phase 7-C / AC26 — pulse-ack log row. The Skill marker
        # (= record_pulse_ack already mutated the trek doc); the log row
        # is the durable observability artefact.
        _append_trek_log_safe(trek_id, {
            "kind": "pulse-ack",
            "session_id": body.session_id,
            "payload": {
                "picked_choice": body.picked_choice or "",
                "note": (body.note or "")[:500],
                "state_summary": (body.state_summary or "")[:500],
                "blockers": list(body.blockers or [])[:20],
                "needs_leader_judgment": bool(body.needs_leader_judgment),
                "time_on_task_seconds": int(body.time_on_task_seconds or 0),
            },
            "created_at": trek_mod.utcnow_iso(),
        })
        return (t.get("pulse_acks") or {}).get(body.session_id) or {}

    @router.post("/api/treks/{trek_id}/extend-ttl")
    def extend_trek_task_ttl_endpoint(trek_id: str, body: TrekExtendTtl,
                                      user: dict = Depends(require_auth)):
        """Postpone the TTL safety net deadline on a single task (ms-95 / e-2308).

        Leader-side primitive for the Agent-tool subagent dispatch path.
        When ``/beacon-dispatch`` launches a subagent that cannot stamp
        ``last_activity_at`` itself (= different ``session_id``, not joined
        to the trek), the leader calls this endpoint to push the auto-stall
        deadline forward by ``minutes`` so the TTL check skips the task
        while delegation is in progress. ``minutes=0`` (or negative) clears
        the extension and lets normal TTL semantics resume.

        Auth: caller must be a trek member. Leader-only is **not** enforced
        so that any joined member can call this on their own behalf (= a
        fork session dispatching a sub-agent for its task without leader
        handoff still has access). Server-side write goes through
        ``trek_mod.extend_task_ttl`` which validates the integer cast.

        Returns the updated ``task_states[task_id]`` entry so the caller
        can echo the new ``ttl_extended_until`` back to the user.
        """
        t = _load_trek_for_read(trek_id, user)
        _reject_if_trek_archived(t)
        if is_auth_enabled():
            uid = user.get("sub") or ""
            if not trek_mod.find_member(t, user_id=uid):
                raise HTTPException(
                    status_code=403,
                    detail="only trek members can extend task TTL",
                )
        if not body.task_id:
            raise HTTPException(status_code=400, detail="task_id required")
        try:
            trek_mod.extend_task_ttl(
                t,
                task_id=body.task_id,
                minutes=body.minutes,
                reason=body.reason or "",
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        db.save_trek(trek_id, t)
        return (t.get("task_states") or {}).get(body.task_id) or {}

    @router.get("/api/treks/{trek_id}/pulse-acks")
    def list_trek_pulse_acks_endpoint(trek_id: str,
                                      user: dict = Depends(require_auth)):
        """Per-session pulse-ack summary for dashboards (ms-88 / e-2108).

        Returns the compact summary built by ``trek_mod.summarize_pulse_acks``
        so the Phase 4 Trek detail page can render compliance widgets without
        pulling the full trek doc. Any joined member may read.
        """
        t = _load_trek_for_read(trek_id, user)
        if is_auth_enabled():
            uid = user.get("sub") or ""
            if not trek_mod.find_member(t, user_id=uid):
                raise HTTPException(
                    status_code=403,
                    detail="only trek members can read pulse-ack stats",
                )
        return trek_mod.summarize_pulse_acks(t)

    @router.patch("/api/treks/{trek_id}/task-state")
    def set_trek_task_state_endpoint(trek_id: str, body: TrekTaskStateSet,
                                     request: Request,
                                     user: dict = Depends(require_auth)):
        """Stamp Trek-internal task state (ms-75 / e-2048).

        Executor sessions call this after each commit / chunk completion
        to declare whether they are still ``working`` on the task, have
        reached ``done``, or need ``waiting-review`` (= user judgment).

        Side effects:
          * Update ``trek_doc.task_states[task_id]`` (= validated transition)
          * Persist trek doc
          * When the stamped state is terminal (= done / waiting-review),
            emit a one-time ``trek-task-review`` bus event addressed to the
            leader's home project so ``/beacon-trek-review`` Skill can
            surface the transition to the human leader (= push model that
            spares the leader from polling).

        Authorisation: caller must be a trek member (= identity at user
        grain). Leader-only is not required — any member session can stamp
        on behalf of the work they performed.
        """
        t = _load_trek_for_read(trek_id, user, request)
        _reject_if_trek_archived(t)
        # Member check. Determine the calling session_id (best-effort —
        # bridges include X-Beacon-Session header; CLI/curl callers may
        # omit it). ms-97 / e-2658 Phase 1 (AC6) — phase A+ trek の時は
        # session_id grain で member check、 pre-A は user_id grain。
        user_id = user.get("sub") or ""
        caller_sid = request.headers.get("X-Beacon-Session", "") or ""
        if not _trek_find_member_dual(t, user_id=user_id, session_id=caller_sid):
            raise HTTPException(
                status_code=403,
                detail="only trek members can stamp task state",
            )
        if not body.task_id:
            raise HTTPException(status_code=400, detail="task_id required")
        try:
            trek_mod.validate_task_state(body.state)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        # ms-128 方針4 (e-4365) — block は edge を張る操作 (add_blocker) 経由でしか
        # 到達できない。task-state から直接 block にすると edge 無しの block が生まれ、
        # reconcile がその edge-less block を待ち先ゼロで復帰させる (= 意図しない待機)。
        # ここで surface で弾き、block は必ず /blocker endpoint 経由に限定する
        # (AX レビュー 2026-07-29 の state/edge desync 指摘)。
        if trek_mod.migrate_legacy_task_state(body.state) == "block":
            raise HTTPException(
                status_code=400,
                detail=("block state is set by drawing a blocker "
                        "(POST /api/treks/{id}/blocker), not via task-state"),
            )
        # ms-97 P5 (= review finding Trek-H3): leader-review gate. The state
        # machine (CORE doc 5nfTSmCDVUzD4SLzIhI5) assigns transitions ORIGINATING
        # from a review-terminal state to a specific role: ``leader_review → *``
        # is the Leader's forced 3-choice review, ``user_review → *`` is the
        # User's call. Without a role gate here, an executor could self-approve
        # its own task (``leader_review → done``) — bypassing the leader review
        # entirely. Require the stamped leader session for those origins.
        # Executor origins (``working → *``) stay open so a member can still
        # stamp the work it actually performed; ``todo``/``done`` re-open paths
        # are likewise unrestricted (not a self-approval vector).
        from_state = trek_mod.get_task_state(t, body.task_id)
        if from_state in ("leader_review", "user_review"):
            _require_trek_leader_session(t, user, request)
        # ms-128 方針5 (e-4366) — done は Trek 状態機械から除去され、書き込みは
        # user_review へ migrate される。以降の「状態の意味」判定 (terminal 判定 /
        # gate 発火 / review-trigger / decision 写像 / event payload) は、client が
        # 送った生の body.state ではなく、実際に保存される effective_state を見る。
        # これで legacy "done" 書き込みも user_review として一貫して扱われ、terminal
        # 集合 (TERMINAL_TASK_STATES) と migration 写像を唯一の真実源にできる
        # (= gate 条件にリテラルタプルを直書きしない)。
        effective_state = trek_mod.migrate_legacy_task_state(body.state)
        # ms-97 / e-2650 — phantom done 構造防御。 Trek slot を terminal に flip
        # する前に、 真値源である project pool の task status が done である
        # ことを必須条件として check する (= 「view 側だけ terminal になる経路」 を
        # server で構造的に reject)。
        # ms-128 方針5 (e-4366) — terminal が done → user_review に移ったので、
        # gate は effective_state が terminal (= user_review) かどうかで発火する。
        # user_review が唯一の terminal (= 「手前まで運んだ」= slot 完了)。 done だけを
        # gate すると literal "user_review" 書き込みが pool-done 検証を素通りして
        # phantom-done の穴が再び開くため、 terminal 集合そのもので gate する
        # (option A、 user 合意 2026-07-28)。 terminal 以外 (todo / working /
        # leader_review への遷移) は状態機械だけで判定する。
        # 旧コード (= 2026-06-28 以前) ではこの check が無く、 e-710 のような
        # 「commit ゼロで Trek slot だけ done」 が成立していた。 ms-97 SPEC
        # AC10 / AC30 補強 + ms-128 方針5 の構造実装、 詳細は lib/trek.py
        # ``check_slot_done_precondition`` の docstring を参照。
        # ms-128 / e-4386 — 完遂ゲート (attainment mode)。user_review (= Trek 唯一の
        # terminal) へ倒す「合格判定」を、実行者の外に固定した全 met の attainment
        # verdict でのみ通す。self_judgment (= 直前に状態を stamp した session が自分で
        # 倒す) と、素の verdict なし approve を構造的に塞ぐ。失敗時は user_review へ
        # 倒さず forced_state へ留置し、scheduler が外部 judge へ review を再通知する。
        # forward-to-user (人間エスカレーション) は gate 対象外。
        if effective_state in trek_mod.TERMINAL_TASK_STATES:
            prior_stamper_sid = (
                (t.get("task_states") or {}).get(body.task_id) or {}
            ).get("updated_by_session_id", "")
            gate = trek_mod.completion_gate_decision(
                effective_state=effective_state,
                from_state=from_state,
                verdict=(body.verdict or ""),
                caller_sid=caller_sid,
                prior_stamper_sid=prior_stamper_sid,
                attainment_verdict=body.attainment_verdict,
            )
            if not gate["allowed"]:
                forced = gate["forced_state"] or "leader_review"
                # forced_state へ divert できる (= 状態機械が from→forced を許す) なら、
                # その遷移として書き込み直して review notify を発火させる (= 留置しつつ
                # leader を再度呼ぶ)。X→X の自己ループは状態機械が許さないので、その時は
                # 書き込まず 409 で弾く (= 現状態のまま留置)。
                if forced != from_state and forced in (
                    trek_mod.VALID_TASK_STATE_TRANSITIONS.get(from_state) or ()
                ):
                    effective_state = forced
                    body.state = forced
                    # note に留置理由を残し、leader の review surface で可視化する。
                    body.note = (
                        f"[attainment gate: {gate['code']}] {gate['message']}\n"
                        + (body.note or "")
                    )[:500]
                else:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": gate["code"],
                            "message": gate["message"],
                            "trek_id": trek_id,
                            "task_id": body.task_id,
                            "forced_state": forced,
                        },
                    )
        if effective_state in trek_mod.TERMINAL_TASK_STATES:
            allowed, reason_code, message = trek_mod.check_slot_done_precondition(
                t,
                task_id=body.task_id,
                get_project=db.get_project,
            )
            if not allowed:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": reason_code,
                        "message": message,
                        "trek_id": trek_id,
                        "task_id": body.task_id,
                    },
                )
        try:
            trek_mod.set_task_state(
                t,
                task_id=body.task_id,
                state=body.state,
                updated_by_session_id=caller_sid,
                note=body.note or "",
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        # ms-99 / e-2834 — clear quiesce marks when a state transitions OUT
        # of terminal. This lets the next quiesce lifecycle fire a fresh DM
        # (= AC "per-quiesce 1 回だけ" is per lifecycle, not per lifetime).
        # The mark stamping happens in the scheduler tick's quiesce branch;
        # here we only reset when a resume-like transition lands.
        if effective_state not in trek_mod.TERMINAL_TASK_STATES:
            meta_after = t.setdefault("meta", {})
            if meta_after.get("quiesced_at"):
                meta_after["quiesced_at"] = None
                meta_after["quiesce_notified_at"] = None
                meta_after["quiesce_reason"] = None
        db.save_trek(trek_id, t)
        # ms-90 / e-3247 + e-3241: リーダーの review 判断 (= leader_review からの
        # 遷移) を decision-event に記録する。done→承認 / user_review→user 転送 /
        # それ以外→再作業。leader_review 遷移時の note はリーダーの review 理由なので
        # rationale (= なぜその判断か) に載せる。executor の作業遷移 (working→*) は
        # 決定ではないので対象外。記録失敗は state 遷移を壊さない (= 付随的)。
        if from_state == "leader_review":
            try:
                _review_pid = _resolve_leader_home_project_id(t)
                if _review_pid:
                    db.append_decision_event(
                        _review_pid,
                        decision_event_mod.decision_event_from_trek_review(
                            decision=decision_event_mod
                            .trek_review_decision_from_state(effective_state),
                            trek_id=trek_id,
                            task_id=body.task_id,
                            decider_session_id=caller_sid,
                            decider_user_id=user_id,
                            agent=decision_event_mod.agent_from_claims(user),
                            rationale=(body.note or None),
                        ),
                    )
            except Exception as _dec_exc:  # pragma: no cover - defensive
                logging.getLogger(__name__).warning(
                    "append_decision_event (trek-review) failed for trek_id=%s: %s",
                    trek_id, _dec_exc,
                )
        # If the new state is terminal, fan out a review-required notice to
        # the leader. The leader's review Skill (/beacon-trek-review)
        # surfaces this as a forced 3-choice action. We use the trek's home
        # project (= first scope entry's project) as the bus event target,
        # and address it to the current leader_session_id so only the
        # responsible session sees it (= no project-wide broadcast).
        # ms-97 / e-2706 (AC1) — review notify trigger 拡張。
        # 旧コードは `TERMINAL_TASK_STATES` (= done / user_review) のみで判定し、
        # `leader_review` 遷移時に trek-task-review event が永久に発火しない構造
        # bug を持っていた (= ms-88 e-2107 で waiting-review → leader_review に
        # 5-state 移行した際の check 条件 drift)。 REVIEW_TRIGGER_STATES (= done /
        # user_review / leader_review) に置き換えて leader への review 通知を
        # 正常化する。 outcome log row は元来 terminal 状態にのみ書く design なので
        # 同じ trigger 集合に乗せる (= leader_review も「leader 判断要請」 という
        # 意味で 1 つの outcome event として記録に値する)。
        if effective_state in trek_mod.REVIEW_TRIGGER_STATES:
            # ms-97 / Phase 7-C / AC26 — outcome log row at review-trigger state.
            # Recorded BEFORE the DM fanout so the log row exists even if
            # leader notification fails (= durable audit trail).
            _append_trek_log_safe(trek_id, {
                "kind": "outcome",
                "session_id": caller_sid,
                "payload": {
                    "task_id": body.task_id,
                    "state": effective_state,
                    "note": (body.note or "")[:500],
                },
                "created_at": trek_mod.utcnow_iso(),
            })
            leader_sid = t.get("leader_session_id") or ""
            scope = t.get("scope") or []
            # ms-97 P4 — resolve the leader's home project so a cross-project
            # leader receives the trek-task-review request (was scope[0] only).
            target_pid = _resolve_leader_home_project_id(t)
            # ms-88 / e-2168 — self-loop suppress: leader が自分で stamp した
            # transition では、 leader 宛 review event を mint しない。 「自分が判断
            # したものを自分にもう一度 review 依頼する」 ループによる envelope mint +
            # DM 配送 + 通知 noise を構造的に排除。 state 遷移自体は normal に
            # 保存されており、 suppress した事実だけ meta.review_suppressions に
            # 残して後から audit 可能化する。
            self_judgment = bool(
                caller_sid and leader_sid and caller_sid == leader_sid
            )
            if self_judgment:
                meta = t.setdefault("meta", {})
                suppressions = meta.setdefault("review_suppressions", [])
                suppressions.append({
                    "task_id": body.task_id,
                    "state": effective_state,
                    "suppression_reason": "self_judgment",
                    "suppressed_at": trek_mod.utcnow_iso(),
                    "caller_session_id": caller_sid,
                })
                try:
                    db.save_trek(trek_id, t)
                except Exception:
                    # Best-effort audit trail; suppression decision still
                    # holds even if save fails.
                    pass
            elif leader_sid and target_pid:
                try:
                    envelope = envelope_mod.issue_t1_system_envelope(
                        project_id=target_pid,
                        trek_id=trek_id,
                        actions_authorized=["trek.task_review"],
                        data_class="free",
                        ttl_seconds=3600,
                    )
                except Exception:
                    envelope = None
                review_payload = {
                    "kind": "trek-task-review",
                    "trek_id": trek_id,
                    "task_id": body.task_id,
                    "state": effective_state,
                    "note": body.note or "",
                    "updated_by_session_id": caller_sid,
                    "recipient_session_id": leader_sid,
                    "body": (
                        f"[Trek task review required] trek_id={trek_id} "
                        f"task_id={body.task_id} state={effective_state}\n"
                        f"executor note: {(body.note or '').strip()[:200]}\n"
                        f"次の action: /beacon-trek-review {trek_id} {body.task_id} "
                        f"で approve / re-work / forward-to-user の 3 択を実行してください。"
                    ),
                    "created_at": trek_mod.utcnow_iso(),
                }
                bus_data = {
                    "channel": "trek-task-review",
                    "sender_session_id": "",
                    "payload": review_payload,
                    "envelope": envelope,
                    "delivery": "auto-execute",
                    "created_at": trek_mod.utcnow_iso(),
                }
                try:
                    db.append_bus_event(target_pid, bus_data)
                except Exception:
                    # Best-effort notification; the leader can still discover
                    # the terminal state via `beacon trek show` polling.
                    pass
        return t

    @router.post("/api/treks/{trek_id}/blocker")
    def add_trek_blocker_endpoint(trek_id: str, body: TrekBlockerSet,
                                  request: Request,
                                  user: dict = Depends(require_auth)):
        """Draw blocker edges ``target_id → blocker_target_ids`` (ms-128 方針4 / e-4365).

        Leader-only: the leader owns the dependency ledger (方針7), so only the
        leader session may declare that one target depends on another. Records each
        edge and transitions the target to ``block``. **Atomic**: all edges apply to
        the in-memory doc first and the doc is saved once — if any edge is rejected
        (self-block / cycle / non-blockable state) nothing is persisted, so cloud and
        local CLI share one all-or-nothing semantics. An already-satisfied blocker is
        a per-edge no-op. Returns the updated trek doc.
        """
        t = _load_trek_for_read(trek_id, user, request)
        _reject_if_trek_archived(t)
        _require_trek_leader_session(t, user, request)
        if not body.target_id:
            raise HTTPException(status_code=400, detail="target_id required")
        ids = list(body.blocker_target_ids or [])
        if body.blocker_target_id:
            ids.append(body.blocker_target_id)
        if not ids:
            raise HTTPException(status_code=400,
                                detail="at least one blocker_target_id required")
        caller_sid = request.headers.get("X-Beacon-Session", "") or ""
        # Apply all to the in-memory doc; save once. Any rejection aborts before save
        # → atomic (no partial persistence).
        for bid in ids:
            try:
                trek_mod.add_blocker(
                    t,
                    target_id=body.target_id,
                    blocker_target_id=bid,
                    updated_by_session_id=caller_sid,
                    note=body.note or "",
                )
            except trek_mod.TrekBlockerError as e:
                raise HTTPException(
                    status_code=_BLOCKER_ERROR_STATUS.get(e.kind, 400),
                    detail={"kind": e.kind, "message": str(e)},
                )
        t["updated_at"] = trek_mod.utcnow_iso()
        db.save_trek(trek_id, t)
        return t

    @router.post("/api/treks/{trek_id}/unblock")
    def remove_trek_blocker_endpoint(trek_id: str, body: TrekUnblockSet,
                                     request: Request,
                                     user: dict = Depends(require_auth)):
        """Remove a blocker edge and reconcile (ms-128 方針4 / e-4365).

        Leader-only. Drops the ``target_id → blocker_target_id`` edge, then runs the
        block reconcile so the target's ``block`` state re-settles (unblocks if that
        was its last unsatisfied blocker). This is the leader's cycle-breaking escape
        hatch. Returns the updated trek doc.
        """
        t = _load_trek_for_read(trek_id, user, request)
        _reject_if_trek_archived(t)
        _require_trek_leader_session(t, user, request)
        if not body.target_id or not body.blocker_target_id:
            raise HTTPException(status_code=400,
                                detail="target_id and blocker_target_id required")
        removed = trek_mod.remove_blocker(
            t, target_id=body.target_id, blocker_target_id=body.blocker_target_id,
        )
        if removed:
            trek_mod.reconcile_blocks(t, updated_by_session_id="server")
            t["updated_at"] = trek_mod.utcnow_iso()
            db.save_trek(trek_id, t)
        return t

    @router.post("/api/treks/{trek_id}/task-add")
    def add_trek_task_endpoint(
        trek_id: str,
        body: TrekTaskAddRequest,
        request: Request,
        user: dict = Depends(require_auth),
    ):
        """Cross-project task add through Trek scope (ms-92 / e-2141).

        Walks ``trek.check_trek_task_add_allowed(t, target_project,
        target_milestone)`` first. On allowed: writes the task to the
        target project's milestone via the existing ``core.task_add``
        primitive, stamping ``meta.trek_id`` for audit traceability. On
        rejected: 403 with the scope-guard reason code so callers can
        show the right remediation hint.

        Authorisation: caller must be a trek member (= same as
        ``task-state``). The scope-guard does the cross-project
        authorisation; the membership check is the "are you allowed to
        drive this Trek at all" gate.

        Reason codes returned in the 403 detail (= mirror the lib/trek.py
        constants so the CLI / docs share a vocabulary):
          * ``project_not_in_scope`` — target_project absent from scope
          * ``milestone_not_in_scope`` — project present, MS not enumerated
          * ``scope_only_has_task_narrowing`` — AC #4 of e-2141 (= MS-grain
            enforcement; task-level scope entries don't sprout sideways)
        """
        t = _load_trek_for_read(trek_id, user)
        _reject_if_trek_archived(t)
        user_id = user.get("sub") or ""
        if is_auth_enabled() and not trek_mod.find_member(t, user_id=user_id):
            raise HTTPException(
                status_code=403,
                detail="only trek members can add tasks through trek scope",
            )
        if not body.target_project:
            raise HTTPException(status_code=400, detail="target_project required")
        if not body.target_milestone:
            raise HTTPException(status_code=400, detail="target_milestone required")
        if not body.description:
            raise HTTPException(status_code=400, detail="description required")
        if not body.target_milestone.startswith("ms-"):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"target_milestone {body.target_milestone!r} must start with "
                    "'ms-' (operations / single tasks are not valid task-add "
                    "targets — see SPEC ms-92 e-2141 AC #4 MS-grain enforcement)"
                ),
            )
        # ms-95 / Trek task-add slug ↔ full project_id resolution.
        # Two storage forms can appear in trek scope: full id (= canonical,
        # what apply_operation needs) or slug (= what users type at
        # ``beacon trek plan --add-scope <slug>:<ms>``). Canonicalise both
        # the request side and an in-memory copy of the scope so the scope
        # match runs on equal footing and the downstream apply_op call
        # always receives a real project_id. Without this the bug from PE
        # + LPS dogfood reports manifests as:
        #   * slug stored + slug request → scope match ok, then 500
        #     (LookupError from apply_operation seeing an unknown id)
        #   * slug stored + full id request → 403 project_not_in_scope
        #     (literal string equality miss)
        requesting_user_id = user.get("sub") or ""
        resolved_target_project = _resolve_canonical_project_id(
            body.target_project, user_id=requesting_user_id,
        )
        if resolved_target_project is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"target_project {body.target_project!r} not found or "
                    "ambiguous (no exact full project_id match and no unique "
                    "slug expansion). Pass the full project_id (e.g. "
                    "'<slug>-<hex6>') if multiple projects share the slug."
                ),
            )
        # Build a scope-copy where each entry's ``project`` field is
        # canonicalised. Unresolvable entries (= stale slug for a deleted
        # project, or a slug shared by multiple of the caller's projects)
        # are kept as-is so they cleanly fail the scope match instead of
        # crashing the endpoint.
        canonical_scope: list[dict] = []
        for s in t.get("scope") or []:
            original = s.get("project") or ""
            resolved = (
                _resolve_canonical_project_id(original, user_id=requesting_user_id)
                or original
            )
            canonical_scope.append({**s, "project": resolved})
        canonical_t = {**t, "scope": canonical_scope}
        # ms-97 Phase 4 / AC14 — executor の home project check.
        #
        # Trek scope を介した slot 起票は、 caller (= executor) が「自分の
        # home project に slot を生やす」 経路に限定する。 すなわち caller
        # の live session_id が登録されている scope project = 起票先 project
        # でなければならない。 違反例 (= executor A が executor B の project に
        # slot を生やす) は executor 間で意図しない作業押し付けを生むので、
        # server side で物理的に塞ぐ (= プロンプト notice ではなく code 制約)。
        #
        # 適用範囲: phase A+ trek のみ (= is_session_id_keyed True)。
        # Pre-A trek は session_id を持たない時代の trek なので、 caller
        # session_id を home に逆引きする手立てが無い → legacy 振る舞い継続
        # (= scope guard だけが authorisation の砦)。
        #
        # X-Beacon-Session header が空の caller (= CLI smoke / 古い client)
        # は home check を skip。 header は opt-in、 hard-require には
        # しない (= AC13 の helper と同じ stance)。
        #
        # leader (= role == "leader") は home check 免除。 leader は全
        # scope projects に対して slot 起票する authority を持つ (=
        # cross-project authority は leader / scope policy 側で行使)。
        if is_auth_enabled() and trek_mod.is_session_id_keyed(canonical_t):
            caller_sid = request.headers.get("X-Beacon-Session", "") or ""
            if caller_sid:
                caller_role = _trek_member_role(
                    canonical_t, user_id=user_id, session_id=caller_sid,
                )
                if caller_role != "leader":
                    scope_project_ids: list[str] = []
                    for s in canonical_scope:
                        pid = s.get("project") or ""
                        if pid and pid not in scope_project_ids:
                            scope_project_ids.append(pid)
                    home_pid = trek_mod.resolve_session_home_project(
                        caller_sid,
                        scope_project_ids,
                        db.list_sessions,
                    )
                    if not home_pid:
                        raise HTTPException(
                            status_code=403,
                            detail=(
                                "executor session is not registered in any "
                                "scope project; cannot resolve home project "
                                "for slot 起票 (AC14)"
                            ),
                        )
                    if home_pid != resolved_target_project:
                        raise HTTPException(
                            status_code=403,
                            detail=(
                                f"executor can only add a slot in their home "
                                f"project (home={home_pid}, "
                                f"target={resolved_target_project}); leader "
                                "must originate cross-project slots (AC14)"
                            ),
                        )
        allowed, reason = trek_mod.check_trek_task_add_allowed(
            canonical_t,
            target_project=resolved_target_project,
            target_milestone=body.target_milestone,
        )
        if not allowed:
            raise HTTPException(
                status_code=403,
                detail=f"trek scope rejects task add: {reason}",
            )
        # Scope check passed. Write to the target project via the existing
        # task_add op. We stamp meta.trek_id on the entry (= audit trail).
        author = _resolve_author(user)

        def op(data: dict):
            # ms-81 / e-1916 write-status gate is intentionally NOT applied
            # here — the Trek scope authorisation is the relevant gate for
            # cross-project writes, and Trek scope only enumerates active
            # work. If the target MS is in a bad write status the entry-add
            # CLI surfaces that locally; cross-project we trust the Trek
            # scope owner's intent.
            try:
                # ms-126: a Trek executor sprouting a task is a machine path — it
                # may not have judged priority. If it supplies one, we use it; if
                # empty, allow_untriaged=True records the ``untriaged`` sentinel as
                # visible debt rather than rejecting the autonomous write.
                eid = core.task_add(
                    data, body.target_milestone, body.description,
                    entry_type=body.type, priority=body.priority,
                    motivation=body.motivation,
                    acceptance_criteria=body.acceptance_criteria,
                    author=author,
                    allow_untriaged=True,
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            # Stamp meta.trek_id on the freshly-created entry so future
            # /beacon-retrospect queries can answer "which tasks were
            # sprouted via Trek X". core.task_add returns just the id, so
            # we re-find the entry and patch its meta.
            find_result = core.find_entry(data, eid)
            if find_result:
                _, _, entry, _ = find_result
                meta = entry.setdefault("meta", {})
                meta["trek_id"] = trek_id
                meta["origin"] = "trek.task_add"
            return data, {
                "entry_id": eid,
                "project_id": resolved_target_project,
                "milestone_id": body.target_milestone,
                "trek_id": trek_id,
            }
        return _apply_op_and_broadcast(
            resolved_target_project,
            op,
            op_name="entry.create.trek_scope",
            actor=user.get("sub", ""),
        )

    @router.post("/api/treks/{trek_id}/reconcile")
    def reconcile_trek_endpoint(trek_id: str, body: TrekReconcileRequest,
                                user: dict = Depends(require_auth)):
        """ms-88 / e-2167 — Trek task_states を task pool と整合させる reconcile.

        Trek 内に既に stamp 済の各 task について scope project の task pool を
        引いて、 「pool 上は done だが Trek stamp は non-terminal で残ってる」
        stuck 状態を一覧化する。 ``apply=true`` なら mirror で done に書き換え。
        ``apply=false`` (= default) は dry-run、 diff だけ返す。

        Authorisation: caller must be a trek member.
        """
        t = _load_trek_for_read(trek_id, user)
        user_id = user.get("sub") or ""
        if not trek_mod.find_member(t, user_id=user_id):
            raise HTTPException(
                status_code=403,
                detail="only trek members can reconcile task state",
            )
        states = t.get("task_states") or {}
        scope_project_ids = [
            s.get("project") for s in t.get("scope") or [] if s.get("project")
        ]
        # Build a single dict of entry_id → pool_status across scope projects.
        pool_status: dict[str, str] = {}
        for pid in scope_project_ids:
            try:
                project_data = db.get_project(pid)
            except Exception:
                continue
            if not project_data:
                continue
            for entry_id in states.keys():
                if entry_id in pool_status:
                    continue
                found = core.find_entry(project_data, entry_id)
                if found:
                    _, _, entry, _ = found
                    pool_status[entry_id] = (entry or {}).get("status") or ""
        # Compute diff: pool done but trek state non-terminal.
        diff: list[dict] = []
        for entry_id, entry in states.items():
            try:
                current_state = trek_mod.get_task_state(t, entry_id)
            except Exception:
                current_state = (entry or {}).get("state") or ""
            pool = pool_status.get(entry_id, "")
            if pool == "done" and current_state not in trek_mod.TERMINAL_TASK_STATES:
                diff.append({
                    "entry_id": entry_id,
                    "trek_state": current_state,
                    "pool_status": pool,
                    "would_change_to": "done",
                })
        applied: list[str] = []
        if body.apply and diff:
            now_iso = trek_mod.utcnow_iso()
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
                        "task pool で done 化、 mirror 同期 (= ms-88 / e-2167 "
                        "reconcile)"
                    ),
                }
                applied.append(entry_id)
            t["task_states"] = states
            t["updated_at"] = now_iso
            try:
                db.save_trek(trek_id, t)
            except Exception as e:
                raise HTTPException(
                    status_code=500,
                    detail=f"trek save failed: {type(e).__name__}: {e}",
                )
        return {
            "trek_id": trek_id,
            "applied": body.apply,
            "diff": diff,
            "applied_entry_ids": applied,
        }

    @router.get("/api/treks/{trek_id}/documents")
    def list_trek_documents_endpoint(trek_id: str,
                                     user: dict = Depends(require_auth)):
        """List documents associated with this trek (= ``trek_id`` field set).

        Iterates the trek's scope to collect candidate projects, then lists
        documents for each and filters by ``trek_id``. Returns the docs the
        caller can see (= the caller is already a trek member, so they have
        visibility into any project that the trek's scope includes).
        """
        t = _load_trek_for_read(trek_id, user)
        out: list = []
        seen_doc_ids: set[str] = set()
        project_ids = {
            s.get("project") for s in t.get("scope") or [] if s.get("project")
        }
        for pid in project_ids:
            try:
                project_docs = db.list_documents(pid)
            except Exception:
                # Stale scope entry pointing at a project the caller cannot
                # read; skip silently so a single bad ref doesn't break the
                # whole listing.
                continue
            for d in project_docs:
                if d.get("trek_id") != trek_id:
                    continue
                doc_id = d.get("doc_id")
                if doc_id in seen_doc_ids:
                    continue
                seen_doc_ids.add(doc_id)
                # Surface the source project_id so the UI can deep-link the doc.
                d_out = dict(d)
                d_out["project_id"] = pid
                out.append(d_out)
        return out

    @router.get("/api/treks/{trek_id}/milestones")
    def list_trek_milestones_endpoint(trek_id: str,
                                      user: dict = Depends(require_auth)):
        """Return milestones in this Trek's scope, walking each scope project.

        Mirrors the /documents pattern: iterate scope → project_ids, load each
        project, pick milestones in-scope (= project-wide entries include all,
        narrowed entries pick by milestone ID), dedupe across projects, attach
        source ``project_id`` on each entry for UI deep-linking.
        """
        t = _load_trek_for_read(trek_id, user)
        by_project = _trek_scope_by_project(t)
        out: list = []
        seen: set[tuple[str, str]] = set()
        for pid, entries in by_project.items():
            try:
                project = _load(pid, user)
            except Exception:
                # Stale scope entry pointing at a project the caller cannot
                # read; skip silently so a single bad ref doesn't break the
                # whole listing (= same regime as /documents).
                continue
            include_all, narrow_ids = _scope_contributes(entries, kind="milestone")
            if not include_all and not narrow_ids:
                # Project is in scope only through operation / task narrowing,
                # so the /milestones aggregate has nothing to contribute here.
                continue
            for ms in project.get("milestones", []) or []:
                ms_id = ms.get("id") or ""
                if not include_all and ms_id not in narrow_ids:
                    continue
                key = (pid, ms_id)
                if key in seen:
                    continue
                seen.add(key)
                ms_out = dict(ms)
                ms_out["project_id"] = pid
                out.append(ms_out)
        return out

    @router.get("/api/treks/{trek_id}/operations")
    def list_trek_operations_endpoint(trek_id: str,
                                      user: dict = Depends(require_auth)):
        """Return Operations in this Trek's scope. Mirrors /milestones."""
        t = _load_trek_for_read(trek_id, user)
        by_project = _trek_scope_by_project(t)
        out: list = []
        seen: set[tuple[str, str]] = set()
        for pid, entries in by_project.items():
            try:
                project = _load(pid, user)
            except Exception:
                continue
            include_all, narrow_ids = _scope_contributes(entries, kind="operation")
            if not include_all and not narrow_ids:
                continue
            for op in project.get("operations", []) or []:
                op_id = op.get("id") or ""
                if not include_all and op_id not in narrow_ids:
                    continue
                key = (pid, op_id)
                if key in seen:
                    continue
                seen.add(key)
                op_out = dict(op)
                op_out["project_id"] = pid
                out.append(op_out)
        return out

    @router.get("/api/treks/{trek_id}/tasks")
    def list_trek_tasks_endpoint(trek_id: str,
                                 user: dict = Depends(require_auth)):
        """Return tasks in this Trek's scope, walking MS / Op containers.

        Task scope sources (= union across all scope entries):
          * project-wide entry → every task in the project
          * milestone entry → every task under that MS
          * operation entry → every task under that Op
          * task entry → that single task

        Each output task carries source ``project_id`` and the immediate
        container reference (``milestone_id`` or ``operation_id``) so the UI
        can render the parent path without re-resolving.
        """
        t = _load_trek_for_read(trek_id, user)
        by_project = _trek_scope_by_project(t)
        out: list = []
        seen: set[tuple[str, str]] = set()
        for pid, entries in by_project.items():
            try:
                project = _load(pid, user)
            except Exception:
                continue
            # Compute per-project inclusion sets for task collection.
            include_all_tasks, _ = _scope_contributes(entries, kind="task")
            # Project-wide scope (no narrowing) also implies all tasks.
            if not include_all_tasks:
                for entry in entries:
                    if not (entry.get("milestone") or entry.get("operation")
                            or entry.get("task")):
                        include_all_tasks = True
                        break
            _, ms_narrow = _scope_contributes(entries, kind="milestone")
            _, op_narrow = _scope_contributes(entries, kind="operation")
            _, task_narrow = _scope_contributes(entries, kind="task")

            def _emit(task: dict, *, milestone_id: str = "",
                      operation_id: str = "") -> None:
                tid = task.get("id") or ""
                if not tid:
                    return
                key = (pid, tid)
                if key in seen:
                    return
                seen.add(key)
                row = dict(task)
                row["project_id"] = pid
                if milestone_id:
                    row["milestone_id"] = milestone_id
                if operation_id:
                    row["operation_id"] = operation_id
                out.append(row)

            for ms in project.get("milestones", []) or []:
                ms_id = ms.get("id") or ""
                ms_wanted = (
                    include_all_tasks or (ms_id and ms_id in ms_narrow)
                )
                for entry in ms.get("entries", []) or []:
                    if entry.get("type") != "task":
                        continue
                    tid = entry.get("id") or ""
                    if ms_wanted or (tid and tid in task_narrow):
                        _emit(entry, milestone_id=ms_id)
            for op in project.get("operations", []) or []:
                op_id = op.get("id") or ""
                op_wanted = (
                    include_all_tasks or (op_id and op_id in op_narrow)
                )
                for entry in op.get("entries", []) or []:
                    if entry.get("type") != "task":
                        continue
                    tid = entry.get("id") or ""
                    if op_wanted or (tid and tid in task_narrow):
                        _emit(entry, operation_id=op_id)
        return out

    @router.get("/api/treks/{trek_id}/scope-entries")
    def list_trek_scope_entries_endpoint(
        trek_id: str,
        user: dict = Depends(require_auth),
    ):
        """Return ``entries[]`` for every MS in this Trek's scope, cross-project.

        Response shape (= one list keyed off the Trek, dedup'd across projects)::

            {
              "trek_id": "tk-xxx",
              "milestones": [
                {
                  "project_id": "lps-abcdef",
                  "milestone_id": "ms-22",
                  "milestone_title": "...",
                  "entries": [ ...entries_to_json shape... ]
                },
                ...
              ]
            }

        The ``entries[]`` shape exactly mirrors
        ``GET /api/projects/{pid}/milestones/{ms_id}/entries`` so the Web UI
        can stuff the result straight into ``state.milestoneEntries[msId]``
        without any per-entry rewriting (= ms-95 / e-2640 AC3).

        Scope semantics (= same rule as ``/api/treks/{id}/milestones`` walker
        above — see ``_scope_contributes``):

          * project-wide entry (= no narrowing key) → every MS in the
            project contributes its entries
          * ``{"project": pid, "milestone": ms_id}`` → just that MS's entries

        Operation- or task-only scope entries do not contribute MS entries
        (= mirrors ``/milestones`` which returns ``[]`` in those cases).

        Stale scope entries pointing at projects the caller cannot read are
        silently skipped (= identical to ``/documents`` / ``/milestones``).
        """
        t = _load_trek_for_read(trek_id, user)
        by_project = _trek_scope_by_project(t)
        out_ms: list = []
        seen: set[tuple[str, str]] = set()
        for pid, entries in by_project.items():
            try:
                project = _load(pid, user)
            except Exception:
                # Stale scope entry pointing at a project the caller cannot
                # read; skip silently so a single bad ref doesn't break the
                # whole listing (= same regime as /documents / /milestones).
                continue
            include_all, narrow_ids = _scope_contributes(entries, kind="milestone")
            if not include_all and not narrow_ids:
                # Project only contributes via operation / task narrowing;
                # the MS aggregate has nothing to ship here.
                continue
            for ms in project.get("milestones", []) or []:
                ms_id = ms.get("id") or ""
                if not include_all and ms_id not in narrow_ids:
                    continue
                key = (pid, ms_id)
                if key in seen:
                    continue
                seen.add(key)
                raw_entries = ms.get("entries", []) or []
                out_ms.append({
                    "project_id": pid,
                    "milestone_id": ms_id,
                    "milestone_title": work_model.target_label(ms),
                    "entries": core.entries_to_json(raw_entries),
                })
        return {
            "trek_id": t.get("trek_id") or trek_id,
            "milestones": out_ms,
        }

    @router.get("/api/treks/{trek_id}/summary")
    def trek_summary_endpoint(trek_id: str, user: dict = Depends(require_auth)):
        """Compact status snapshot for dashboards / Web UI Treks tab.

        Returns the high-level counts + status fields without exposing the full
        members[] / scope[] arrays — a separate GET /api/treks/{id} fetches the
        full doc when the caller drills in.
        """
        t = _load_trek_for_read(trek_id, user)
        members = t.get("members") or []
        return {
            "trek_id": t.get("trek_id"),
            "title": t.get("title"),
            "type": t.get("type"),
            "status": t.get("status"),
            "halted": bool(t.get("halt")),
            "halt": t.get("halt"),
            "leader_session_id": t.get("leader_session_id"),
            "creator_actor": t.get("creator_actor"),
            "member_count": len(members),
            "joined_member_count": sum(1 for m in members if m.get("joined_at")),
            "scope_count": len(t.get("scope") or []),
            "created_at": t.get("created_at"),
            "updated_at": t.get("updated_at"),
            "archived_at": t.get("archived_at"),
        }

    @router.post("/api/treks/{trek_id}/session-heartbeat")
    def trek_session_heartbeat(
        trek_id: str,
        body: TrekHeartbeatRequest,
        user: dict = Depends(require_auth),
    ):
        """Stamp the trek's last_session_response_at (ms-83 / e-2001)."""
        t = _load_trek_for_read(trek_id, user)
        _reject_if_trek_archived(t)
        _require_trek_joined_member(t, user)
        import datetime
        now_iso = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        meta = t.setdefault("meta", {})
        meta["last_session_response_at"] = now_iso
        if body.session_id:
            meta["last_session_response_session_id"] = body.session_id
        t["updated_at"] = trek_mod.utcnow_iso()
        db.save_trek(trek_id, t)
        return {
            "trek_id": trek_id,
            "last_session_response_at": now_iso,
        }

    @router.get("/api/system/trek-internal-send")
    def trek_internal_send_endpoint(
        sender_project_id: str,
        sender_session_id: str,
        recipient_session_id: str,
        recipient_project_id: str = "",
        user: dict = Depends(require_auth),
    ):
        """Answer whether an autonomous DM reply is Trek-internal (e-4116 / ms-75).

        The MCP reply path (``channel/bus.mjs``) consumes the auto-reply budget
        before posting, but a reply between two members of the same active Trek
        must NOT cost budget — a Trek is a pre-approved scope with its own TTL /
        halt controls, so the runaway-cap is structurally redundant for
        member-to-member coordination. Before e-4116 the MCP path had no Trek
        awareness at all, so leader↔executor DMs exhausted the budget and Trek
        coordination deadlocked (observed 2026-07-24). bus.mjs cannot see Trek
        membership (it is server-side state), so it pre-flights this endpoint
        before consuming budget.

        Single source of truth: reuses ``dm_gate``'s session-grain shared-Trek
        lookup (``build_shared_trek_lookup_from_lists``) — the SAME rule the
        receiver-side action gate applies (halt / archived filtered, phase-A
        session grain, live per-call trek fetch). Covers cross-project replies via
        ``recipient_project_id``.

        Read-only. Best-effort: returns ``trek_internal=false`` on any unresolved
        id so a failed lookup keeps the budget gate in force — never a silent
        relaxation.

        Requires auth (PR #491 review 2a): although it returns only a boolean +
        trek_id, an *unauthenticated* endpoint would be a membership oracle — anyone
        holding two session ids could probe whether they share a Trek (and get the
        trek_id), and hammer the per-call ``list_sessions`` + ``list_treks`` scan as
        a cheap DoS amplifier. Gating it behind ``require_auth`` (the MCP bridge
        already sends its bearer token) removes the anonymous oracle. The real
        action-authorization boundary remains the dm_gate check on the bus POST;
        this endpoint only drives a *client-side* budget optimization.
        """
        if not sender_session_id or not recipient_session_id:
            return {"trek_internal": False, "trek_id": ""}
        rpid = recipient_project_id or sender_project_id
        sender_uid = _sid_to_uid(sender_project_id, sender_session_id)
        receiver_uid = _sid_to_uid(rpid, recipient_session_id)
        if not sender_uid or not receiver_uid:
            return {"trek_internal": False, "trek_id": ""}
        lookup = dm_gate_mod.build_shared_trek_lookup_from_lists(
            lambda uid: db.list_treks(actor_id=uid) if uid else [],
        )
        matched, trek_id = dm_gate_mod._coerce_lookup_result(
            lookup(sender_uid, receiver_uid,
                   sender_session_id, recipient_session_id)
        )
        return {"trek_internal": bool(matched), "trek_id": trek_id or ""}

    return router
