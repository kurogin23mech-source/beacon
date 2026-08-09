"""Projects ("/api/projects/*") router — PR1: core CRUD + milestones + entries +
operations + documents + claims + changelog + log/summary.

ms-127 e-4871 (B フェーズ・最終, sub-resource split PR1 of 3): the first slice of
the projects god-resource extracted from server/app.py, following the
make_router(require_auth, *, ...injected helpers) pattern established by
routers_me / routers_orgs / routers_admin / routers_auth / routers_treks.

Pure move: every route body + helper is verbatim from app.py. Two mechanical
deltas only: ``@app.<m>`` -> ``@router.<m>``, and the single read of app.py's
module-global ``_auth_enabled`` bool (in put_project) -> the injected
``is_auth_enabled()`` getter (so tests that flip the shared flag at runtime are
reflected — same reasoning as routers_auth's get_local_dev_enabled).

Split boundary (this PR): PR2 (members / invitations / rehome / notes / sessions
/ session_logs / retros / search) and PR3 (bus / dm) stay in app.py for now. The
project auth-guard family (``_load`` / ``_require_project_role`` /
``_require_write`` / ``_require_owner`` / ``_load_meta_only``) is NOT moved — it
is shared across all three slices and reads ``_auth_enabled``, so it stays owned
by app.py and is INJECTED into every make_router. This keeps the slices
independent (no circular imports), the same resolution used for the trek guards.

Module-level helpers below are self-contained (depend only on db / core /
operations / trek_mod / envelope_mod / master_adapter / work_model) — no auth
flag, no injected app.py callables — so they live at module scope. All route
handlers are nested inside make_router so they can close over the injected deps.

Injected dependencies (owned by app.py, passed in to avoid an import cycle):

- ``require_auth``                  — identity dependency.
- ``_load`` / ``_load_meta_only``   — project load (+ role check) / meta-only load.
- ``_require_project_role`` / ``_require_write`` / ``_require_owner`` — RBAC guards.
- ``_apply_op_and_broadcast``       — the write + WS-broadcast path (~15 routes).
- ``_resolve_author``               — authorship resolution.
- ``_save``                         — project persist.
- ``_broadcast_project_after_write`` / ``_broadcast_document_change`` — WS pushes
                                       (own app.py's _ws_connections / _event_loop).
- ``require_envelope_for_action``   — envelope-gate dependency factory (owns
                                       app.py's nonce / parent-lookup stores).
- ``is_auth_enabled``               — zero-arg getter for the ``_auth_enabled`` bool.

``db`` mirrors app.py's binding — ``store_router as db`` (e-1544 backend routing).
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import store_router as db  # e-1544: same backend-routing binding app.py uses
import core
import operations
import trek as trek_mod
import envelope as envelope_mod
import work_model
import master_adapter
import approved_actions as approved_actions_mod
import disclosure as disclosure_mod
import master_binding
import master_projection
import org as org_mod
import phantom_done_evidence as phantom_done_mod
import invitations as invitations_mod  # ms-127 e-4871 PR2: token-based invites
import datetime
import decision_event as decision_event_mod  # ms-90 / e-3246: decision-event 記録

# Structured-audit logger (name-based singleton — same object app.py binds).
# _check_phantom_done_evidence emits its phantom-done warning here.
_audit_logger = logging.getLogger("beacon.audit")


# ---------------------------------------------------------------------------
# Pydantic request models (project-only; verbatim from app.py)
# ---------------------------------------------------------------------------

class ProjectCreate(BaseModel):
    name: str
    objective: str = ""

class MilestoneCreate(BaseModel):
    title: str
    target_date: str = ""
    description: str = ""
    priority: str = ""
    objective: str = ""
    acceptance_criteria: str = ""

class MilestoneUpdate(BaseModel):
    title: str = ""
    progress: str = ""
    target_date: str = ""
    status: str = ""
    description: str = ""
    # ms-126 (AX round-2): model "unset" (None = field omitted = no change)
    # distinctly from an explicit value, and disclose optionality in the schema.
    # None → leave the priority untouched; a provided value must be one of the 5
    # severities (untriaged is a machine sentinel, rejected as not-a-severity).
    priority: Optional[str] = None
    objective: str = ""
    acceptance_criteria: str = ""

class EntryCreate(BaseModel):
    description: str
    type: str = "task"
    date: str = ""
    detail: str = ""
    # ms-126: priority is required for task entries (the human web form must
    # supply one of the 5 severities). Empty + type=="task" is rejected by
    # core.task_add and surfaced as a 400 below. Non-task entries (commit /
    # note) ignore it.
    priority: str = ""

class EntryUpdate(BaseModel):
    description: str = ""
    status: str = ""
    detail: str = ""
    date: str = ""
    # ms-126 (e-4224 + AX round-2): the untriaged-recovery path must exist on
    # the web surface too — a non-terminal human triaging a machine-created
    # untriaged task PATCHes a real severity here. Modelled as Optional so the
    # schema DISCLOSES the value domain and distinguishes "unset" (None = field
    # omitted = no change) from an explicit value. A provided value must be one
    # of the 5 severities; ``untriaged`` is a machine sentinel and is rejected
    # state-independently (you cannot re-assert it from a client — omit the
    # field to leave an untriaged task unchanged). This removes the earlier
    # state-dependent "echo untriaged = no-op / else 400" ambiguity.
    priority: Optional[str] = None

class LogCommit(BaseModel):
    hash: str
    message: str
    date: str
    summary: str = ""
    ms_id: str = ""
    progress: str = ""

class SummaryUpdate(BaseModel):
    text: str

class OperationApproveRequest(BaseModel):
    """Body for POST /api/projects/{id}/operations/{op_id}/envelopes (ms-60 / e-1339).

    Mints a T2 envelope from a SPEC doc whose frontmatter declares
    ``approved_actions``. The SPEC doc must already exist and be linked to
    ``op_id`` (frontmatter ``operation: op-X``). ``ttl_seconds`` defaults to
    "effectively forever" (30 years) per ms-60 SPEC § 設計方針 2 —
    "SPEC 更新まで無期限" with explicit ``beacon operation revoke`` as the
    escape valve.
    """
    spec_doc_id: str
    ttl_seconds: int = 30 * 365 * 86400  # ~30 years; revoke is the kill-switch

class OperationRevokeRequest(BaseModel):
    """Body for POST /api/projects/{id}/operations/{op_id}/envelopes/{env_id}/revoke."""
    reason: str = "manual revoke"

class OperationFireClaimRequest(BaseModel):
    """Body for POST /api/projects/{id}/operation-fires/{op_id}/claim (ms-95).

    Atomic per-day claim used by the CLI scheduler to dedup operation
    triggers across parallel bclaude sessions in the same project. See
    ``claim_operation_fire_if_new`` in firestore_client for the gate
    semantics. ``session_id`` is informational (= which session won the
    race today) and may be empty when the caller has no bridge mint.
    """
    session_id: str = ""

class DocumentSave(BaseModel):
    title: str
    content: str
    scope: Optional[str] = None  # core | spec | memo

class DeleteRequest(BaseModel):
    reason: str = ""

class ActiveClaimSave(BaseModel):
    """Body for ``POST /api/projects/{pid}/active_claims/{claim_id}`` (ms-55 e-1730).

    The whole `payload` dict is the wire shape lib/claims.py:build_claim_payload
    produces — claim_kind, target {kind,id}, from_session_id, intent,
    optional to_session_id / expires_at / metadata, issued_at, claim_id.
    We do not validate the schema server-side; the client builds + validates
    locally and this layer is a pure persistence mirror.
    """
    payload: dict

class PurgeRequest(BaseModel):
    """Body for destructive hard-delete endpoints (milestone/entry/operation purge).

    `reason` is required (audit trail per CORE doc data-immutability-principle).
    `index` (1-based) disambiguates when duplicate IDs exist — set to None when
    only a single record matches.
    """
    reason: str
    index: Optional[int] = None


# ---------------------------------------------------------------------------
# Module-level project helpers (self-contained: db / core / operations /
# trek_mod / envelope_mod / master_adapter). Verbatim from app.py.
# ---------------------------------------------------------------------------

def _master_linking_enabled() -> bool:
    """master linking (ms-111 linking go-live) が本番活性化されているか (default OFF)。

    本番投下は user のゲート (SPEC ms-111 安全策)。この env flag を deploy 時に明示 ON に
    するまで、server ingest は投影を master に link せず従来どおり (= projection が唯一の
    source、regression なし)。ON にすると put_project ingest が全 Account を Beacon-default
    master に link し、read 側 resolver (既配線) が master 真値を返すようになる。
    """
    return os.environ.get("BEACON_MASTER_LINKING_ENABLED", "").strip().lower() in (
        "1", "true", "yes", "on")

def _link_body_accounts_to_master(body: dict) -> None:
    """whole-project write ingest の choke point で全 Account/Contact を master に link する
    (ms-111 e-3621 chunk2b / AC1・AC2, flag-gated)。

    linking の go-live は user ゲート (``_master_linking_enabled``)。ON の時だけ:
      1. project の master_binding (AC2 / SPEC §5・§8) を resolve し束縛軸 org_id と system
         を得る。Beacon-default master 以外 (外部 CRM = 未実装) と org 未定 ("") は対象外で skip。
      2. backend 配線済み adapter (server 側のみ持てる) で全 Account/Contact を link する
         (AC1 = project DB を external_ref 参照化)。

    CLI は backend adapter を持てない (store_router は server 専用) ので linking は必ずこの
    server ingest に集約する = 「一部 site だけ master 経由」の部分 swap を構造的に防ぐ。
    失敗は write を壊してはならない (投影は既に valid)。observe 用に log するだけ。
    """
    if not _master_linking_enabled():
        return
    try:
        binding = master_binding.resolve_master_binding(body)
        system = binding.get("system", "")
        org_id = binding.get("org_id", "")
        # 本 MS の scope は Beacon-default master のみ (外部 CRM adapter は未実装)。
        # org 未定 ("") は束縛軸が無く link 不可 → どちらも従来どおり投影のまま。
        if system != master_binding.BEACON_DEFAULT_SYSTEM or not org_id:
            return
        adapter = master_adapter.get_master_adapter()
        result = master_projection.link_project_accounts(
            body, adapter, org_id=org_id, now=core._now_iso(), system=system)
        if result.get("skipped"):
            logging.getLogger(__name__).info(
                "master linking skipped entries=%s", result["skipped"])
    except Exception as exc:  # linking must never break the project write
        logging.getLogger(__name__).warning("master linking failed: %s", exc)

def _mirror_task_done_to_treks(entry_id: str) -> list[str]:
    """ms-88 / e-2167 — task pool ↔ Trek stamp 同期 (= mirror sync).

    task pool 側で task が ``done`` に成った瞬間に、 active な Trek の
    ``task_states[<entry_id>]`` が ``working`` / ``todo`` で stamp 済 なら
    自動で ``done`` に mirror する。 「task pool で done だが Trek stamp は
    working で残ってる」 stuck 状態 (= 2026-06-19 dogfood の e-2045 14h
    放置事例) を構造的に排除する。

    ms-97 P5 (= review Trek-H3): ``leader_review`` は除外する。 これは
    executor が忘れた stuck stamp ではなく leader の forced review を待つ
    意図的な状態なので、 pool done で ``done`` に上書きすると leader review
    を bypass してしまう (= endpoint 側の P5 gate と同じ穴)。

    state transition validation は bypass する (= 直接書き換え)。 これは
    server-forced reconciliation であり、 「executor / leader / user が
    意図的に state を進める」 normal transition とは性質が違う。

    Returns trek_ids that were touched.
    """
    touched: list[str] = []
    try:
        all_treks = db.list_treks(actor_id=None)
    except Exception:
        return touched
    for t in all_treks:
        if t.get("status") != "active":
            continue
        states = t.get("task_states") or {}
        existing = states.get(entry_id)
        if not existing:
            continue
        try:
            current_state = trek_mod.get_task_state(t, entry_id)
        except Exception:
            current_state = (existing or {}).get("state") or ""
        if current_state in trek_mod.TERMINAL_TASK_STATES:
            continue
        # ms-97 P5 (= review finding Trek-H3): never auto-mirror a task that
        # is awaiting the leader's forced review. ``leader_review`` is a
        # deliberate "human judgment pending" state, not a stuck stamp the
        # executor forgot to advance — overwriting it to ``done`` on pool sync
        # bypasses the leader review the same way an executor self-approve
        # would. The mirror only exists to unstick ``working`` / ``todo``
        # stamps left behind when the pool moved on; leave the review gate to
        # the leader (via /beacon-trek-review + the endpoint's P5 gate above).
        if current_state == "leader_review":
            continue
        # Direct mirror write (= bypass transition validation).
        now_iso = trek_mod.utcnow_iso()
        states[entry_id] = {
            **existing,
            "state": "done",
            "updated_at": now_iso,
            "last_activity_at": now_iso,
            "updated_by_session_id": "task-pool-mirror",
            "note": (
                "task pool で done 化、 mirror 同期 (= ms-88 / e-2167)"
            ),
        }
        t["task_states"] = states
        t["updated_at"] = now_iso
        try:
            db.save_trek(t.get("trek_id", ""), t)
            touched.append(t.get("trek_id", ""))
        except Exception:
            continue
    return touched

def _check_phantom_done_evidence(
    project_id: str,
    entry_id: str,
    user: dict,
    request: Optional[Request] = None,
) -> Optional[dict]:
    """Evaluate a freshly-done task for commit evidence; emit warning if missing.

    Returns the assessment dict (or ``None`` on lookup failure / disabled).
    Failure is silently swallowed — the warning emission is purely
    observational and must not block the task done response. The returned
    assessment is folded into the endpoint response so the CLI can surface
    the warning to the user in the same turn.

    Disabled via env ``BEACON_PHANTOM_DONE_GATE=0`` (escape hatch for
    Cloud Run rollback without a redeploy). Default = enabled.
    """
    if os.environ.get("BEACON_PHANTOM_DONE_GATE", "1") == "0":
        return None
    try:
        data = operations.load_project_consistent(project_id)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    found = core.find_entry(data, entry_id)
    if not found:
        return None
    _, _, entry, _ = found
    recent_commits = phantom_done_mod.collect_recent_commits(data)
    assessment = phantom_done_mod.evaluate_done_evidence(entry, recent_commits)
    if assessment.get("has_evidence"):
        # All good — task has a matching commit OR has no keywords to
        # judge against. Do not pollute Cloud Logging with negatives.
        return assessment
    # Phantom done detected — emit structured warning.
    sid = ""
    if request is not None:
        try:
            sid = request.headers.get("X-Beacon-Session", "") or ""
        except Exception:
            sid = ""
    uid = ""
    if isinstance(user, dict):
        uid = user.get("sub") or user.get("email") or ""
    log_record = {
        "evt": "task.done.phantom_done_warning",
        "severity": "WARNING",
        "phantom_done_warning": True,  # flag for Cloud Logging filter
        "task_id": entry_id,
        "project_id": project_id,
        "user_id": uid,
        "session_id": sid,
        "task_description": (entry.get("description") or "")[:200],
        "task_keywords_sample": assessment.get("task_keywords", [])[:20],
        "commit_window": assessment.get("window", 0),
        "threshold": assessment.get("threshold"),
        "match_type": assessment.get("match_type", "none"),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        # severity=WARNING so Cloud Logging splits this from the bulk
        # INFO-level audit traffic. The json payload is what aggregation
        # queries key off.
        _audit_logger.warning(json.dumps(log_record, ensure_ascii=False))
    except Exception:
        pass
    return assessment

def _spec_doc_for_op(project_id: str, op_id: str, spec_doc_id: str) -> dict:
    """Load a SPEC doc and verify it's bound to ``op_id``.

    Raises HTTPException with a clear reason on any failure path so the CLI
    can surface a useful message rather than a generic 500.
    """
    doc = db.get_document(project_id, spec_doc_id)
    if not doc:
        raise HTTPException(
            status_code=404, detail=f"SPEC doc not found: {spec_doc_id}"
        )
    content = doc.get("content", "") or ""
    declared_scope = db._extract_frontmatter_field(content, "scope")
    if declared_scope != "spec":
        raise HTTPException(
            status_code=400,
            detail=(f"doc {spec_doc_id} has scope={declared_scope!r}, "
                    f"expected 'spec'"),
        )
    declared_op = db._extract_frontmatter_field(content, "operation")
    if declared_op != op_id:
        raise HTTPException(
            status_code=400,
            detail=(f"SPEC doc {spec_doc_id} is bound to operation "
                    f"{declared_op!r}, not {op_id!r}"),
        )
    return doc

def _enrich_project(data: dict) -> dict:
    """Add computed fields (total_tasks, done_tasks, entries_to_json) to project."""
    enriched = {**data}
    milestones = []
    for ms in data.get("milestones", []):
        entries = ms.get("entries", [])
        total, done = core.count_task_status(entries)
        milestones.append({
            **ms,
            "entries": core.entries_to_json(entries),
            "total_tasks": total,
            "done_tasks": done,
        })
    enriched["milestones"] = milestones
    return enriched

def _enrich_project_slim(data: dict) -> dict:
    """Slim variant for WS broadcast — drops tab-scoped heavy arrays.

    ms-84 / e-2326 (= initial fix) + follow-up: dropping entries[] alone left
    the Beacon project at 413 KB which is still above whatever WS frame size
    Cloud Run / GFE tolerates in practice (= 5 retries of wss:// confirmed
    1006 close at 173 KB and 413 KB, while 32 KB went through). Profiling the
    slim payload pointed at top-level arrays that the dashboard doesn't need:
    pushes 311 KiB / deployments 80 KiB / worktree_sessions 5 KiB. These are
    Releases / Worktree tab content. We drop them from the WS broadcast and
    the Web UI / Tauri fetch them via tab-specific REST endpoints on demand:

      GET /api/projects/{id}/pushes
      GET /api/projects/{id}/deployments
      GET /api/projects/{id}/worktree-sessions

    Milestones keep total_tasks / done_tasks so card summaries stay accurate;
    entries[] is still fetched per-MS on expand via
    GET /api/projects/{id}/milestones/{ms_id}/entries. Operations stays in
    slim because the dashboard renders operation cards inline.

    REST GET /api/projects/{id} (= default, full) is unchanged so CLI / Tauri
    IPC consumers continue to see the complete payload.
    """
    enriched = {**data}
    for tab_scoped in ("pushes", "deployments", "worktree_sessions"):
        enriched.pop(tab_scoped, None)
    milestones = []
    for ms in data.get("milestones", []):
        entries = ms.get("entries", [])
        total, done = core.count_task_status(entries)
        slim_ms = {k: v for k, v in ms.items() if k != "entries"}
        slim_ms["total_tasks"] = total
        slim_ms["done_tasks"] = done
        milestones.append(slim_ms)
    enriched["milestones"] = milestones
    return enriched

def _build_document_change_payload(project_id: str, doc_id: str, op: str,
                                   fallback_title: str = "",
                                   fallback_scope: str | None = None) -> dict:
    """Construct the WS payload for a document add/update event.

    We re-read the saved doc via ``db.get_document`` so the broadcast carries
    the *post-write* values (especially ``updated_at`` which the server stamps
    and ``scope`` which may be normalized from frontmatter). If the read
    fails for any reason — racing delete, transient Firestore error — we
    degrade to the request body values so clients still get something to
    insert/update on, rather than silently dropping the event.
    """
    saved = db.get_document(project_id, doc_id) or {}
    scope = saved.get("scope") or fallback_scope or "memo"
    payload = {
        "op": op,
        "doc_id": doc_id,
        "title": saved.get("title", fallback_title),
        "scope": scope,
        "updated_at": saved.get("updated_at", ""),
    }
    milestone = saved.get("milestone")
    if milestone:
        payload["milestone"] = milestone
    return payload


# ---------------------------------------------------------------------------
# Bus/dm gate — Pydantic models (canonical home; used by make_bus_gate_router)
# ---------------------------------------------------------------------------

class EnvelopeIssueRequest(BaseModel):
    """Body for POST /api/projects/{project_id}/bus/envelope/issue.

    e-1155 Phase 1. The server uses the calling user (require_auth) as
    proof of the human signature for T1, and signs the envelope with the
    server HMAC secret. T2 envelopes (Operation scope) are also issued
    here — the caller declares ``scope`` to opt in.

    Issuance discipline (CORE doc § "scope 自然言語の曖昧性"):
      * ``actions_authorized`` must enumerate concrete action names
      * wildcards / regex / natural language are rejected at the
        envelope module boundary
    """
    tier: str
    actions_authorized: list[str] = []
    scope: Optional[str] = None
    data_class: str = "free"
    conversation_id: Optional[str] = None
    in_reply_to: Optional[str] = None
    chain_depth: int = 0
    ttl_seconds: int = 3600
    # ms-110 / e-3443: optional recipient_confirmed consent claim. When present
    # it is baked into the signed envelope so the claim is authentic (server
    # HMAC covers it) and survives the receive-time verify pipeline. This is
    # how a cross-user DM carries its human recipient-confirmation without
    # breaking the signature (the claim used to be appended after signing,
    # which invalidated it → T5 degrade → 403).
    recipient_confirmed: Optional[dict] = None


class CheckTaskAddRequest(BaseModel):
    """Body for POST /api/projects/{id}/bus/envelope/check-task-add (ms-83 / e-2000).

    Pure verify endpoint: given an envelope and a target MS, return
    whether the AI may add a task autonomously (auto), should propose
    to the user (propose), or must be rejected outright (reject).

    The endpoint runs the regular 9-step envelope verify pipeline first.
    If verify passes, it then evaluates the action × tier matrix for
    ``task.add`` against the target MS. T1 always permits. T2 permits
    when the Operation scope enumerates the MS. T1-system permits when
    the Trek scope (= server-side trek doc) includes the MS. Anything
    else degrades to propose-to-ai.
    """
    envelope: dict
    target_ms: str
    payload: dict = {}


class T1SystemEnvelopeRequest(BaseModel):
    """Body for POST /api/projects/{project_id}/bus/envelope/t1-system/issue.

    ms-83 / e-1995. Used by the server-side scheduler (= the periodic loop
    that fires "next, please" progress-check DMs into a Trek's claim
    session) to mint a T1-equivalent envelope for an active Trek scope.
    The caller must present the shared scheduler key in the
    ``X-Beacon-Scheduler-Key`` header so a user account cannot pose as
    the server.
    """
    trek_id: str
    actions_authorized: list[str] = []
    data_class: str = "free"
    ttl_seconds: int = 3600
    conversation_id: Optional[str] = None


class DMRespondBody(BaseModel):
    decision: str  # "approve" | "deny"
    # ms-90 / e-3247: 承認/却下の背景 (= 直面した問題) と判断理由。decision-event
    # に記録するためのメタデータ。任意 (= 未指定でも決定は通る)。
    context: str = ""
    rationale: str = ""


# ---------------------------------------------------------------------------
# Bus/dm gate — module-level helpers (verbatim from app.py)
# ---------------------------------------------------------------------------

def _get_envelope_nonce_store():
    """Lazy-resolve the in-memory nonce store for the check-task-add gate.

    This is a **gate-local** process-scoped singleton used only by
    ``check_task_add_envelope`` (the pure verify endpoint). It is NOT the
    store the delivery bus-receive flow uses — that path uses app.py's
    ``_FirestoreNonceStore`` via ``_envelope_nonce_store()`` (note the near-
    identical name). Keeping this InMemory singleton module-local means the
    check-task-add verify has a stable replay-protection scope within a
    process; tests reset it by ``delattr``-ing
    ``routers_projects._envelope_nonce_store_singleton``.
    """
    global _envelope_nonce_store_singleton
    try:
        return _envelope_nonce_store_singleton
    except NameError:
        _envelope_nonce_store_singleton = envelope_mod.InMemoryNonceStore()
        return _envelope_nonce_store_singleton


def _get_envelope_parent_lookup():
    """Return a parent_lookup that consults the bus_event store.

    For task.add we never have in_reply_to chains (= scheduler-fired
    envelopes have no parent), so a constant-None lookup is sufficient.
    """
    return envelope_mod.FunctionParentLookup(lambda _pid, _eid: None)


def make_router(
    require_auth: Callable,
    *,
    _load: Callable,
    _load_meta_only: Callable,
    _require_project_role: Callable,
    _require_write: Callable,
    _require_owner: Callable,
    _apply_op_and_broadcast: Callable,
    _resolve_author: Callable,
    _save: Callable,
    _broadcast_project_after_write: Callable,
    _broadcast_document_change: Callable,
    require_envelope_for_action: Callable,
    is_auth_enabled: Callable[[], bool],
) -> APIRouter:
    """Build the /api/projects/* (PR1 slice) router with the host app's
    guards + write helpers injected. Keyword-only + Callable-typed with a
    construction-time callability check (a mis-wire fails at mount, not at
    request time)."""
    for _name, _dep in (
        ("require_auth", require_auth), ("_load", _load),
        ("_load_meta_only", _load_meta_only),
        ("_require_project_role", _require_project_role),
        ("_require_write", _require_write), ("_require_owner", _require_owner),
        ("_apply_op_and_broadcast", _apply_op_and_broadcast),
        ("_resolve_author", _resolve_author), ("_save", _save),
        ("_broadcast_project_after_write", _broadcast_project_after_write),
        ("_broadcast_document_change", _broadcast_document_change),
        ("require_envelope_for_action", require_envelope_for_action),
        ("is_auth_enabled", is_auth_enabled),
    ):
        if not callable(_dep):
            raise TypeError(
                f"routers_projects.make_router: {_name} must be callable, "
                f"got {type(_dep).__name__} — pass a function, not a value."
            )

    router = APIRouter()

    # ---- route handlers (verbatim bodies; @app -> @router) ----

    @router.get("/api/projects/{project_id}/version")
    def get_project_version(project_id: str, user: dict = Depends(require_auth)):
        """Per-project version info derived from push records (e-587).

        Returns:
          {
            "latest_pushed_semver":   "v0.4.0"  | "",   # most recent push that
                                                       # carried an explicit semver
            "latest_pushed_at":       "2026-05-28T..." | "",
            "commits_since_release":  N,         # length of pushes after that one
            "total_pushes":           N,
            "tag":                    "v0.4.0" | "",   # convenience alias
          }

        The Web UI displays this as "v0.4.0  +N commits since release". A
        blank `tag` means the project hasn't started using version-rules yet —
        show nothing rather than a misleading "v?".
        """
        # Permission check — viewers are fine for read, admins bypass membership.
        # e-1257: route through _require_project_role so this endpoint can't drift
        # away from the WS/REST authorization gate. Admin bypass is preserved by
        # short-circuiting the membership check when user.is_admin is set.
        if user.get("is_admin"):
            try:
                data = operations.load_project_consistent(project_id)
            except LookupError:
                raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
        else:
            data, _role = _require_project_role(project_id, user)

        pushes = data.get("pushes") or []
        # `pushes` ordering varies — sort by pushed_at to be safe.
        sortable = []
        for p in pushes:
            if not isinstance(p, dict):
                continue
            sortable.append((p.get("pushed_at", "") or "", p))
        sortable.sort(key=lambda x: x[0], reverse=True)

        latest_semver = ""
        latest_at = ""
        commits_since = 0
        for _, p in sortable:
            meta = p.get("meta") or {}
            sem = p.get("semver") or meta.get("semver") or ""
            if sem and not latest_semver:
                latest_semver = sem
                latest_at = p.get("pushed_at", "")
                break
            commits_since += p.get("commit_count", 0) or 0

        return {
            "latest_pushed_semver": latest_semver,
            "latest_pushed_at": latest_at,
            "commits_since_release": commits_since,
            "total_pushes": len(pushes),
            "tag": latest_semver,
        }

    @router.get("/api/projects")
    def list_projects(include_archived: bool = False, user: dict = Depends(require_auth)):
        """List projects owned by or shared with the current user."""
        return db.list_projects(user_id=user.get("sub"), include_archived=include_archived)

    @router.post("/api/projects/{project_id}/archive")
    def archive_project(
        project_id: str,
        user: dict = Depends(require_auth),
        _envelope: dict = Depends(require_envelope_for_action("project.archive")),
    ):
        """Archive a project (soft delete — hidden from default listing)."""
        # e-1257: owner-only gate via the centralized helper (404 if missing,
        # 403 if not owner). Pre-check before the transaction mirrors the pattern
        # used by envelope issuance (L2131) and other owner-gated mutations.
        _require_project_role(project_id, user, allowed=("owner",))
        def op(data: dict):
            data["archived"] = True
            return data, {"status": "archived", "project_id": project_id}
        return _apply_op_and_broadcast(
            project_id, op, op_name="project.archive", actor=user.get("sub", ""),
        )

    @router.post("/api/projects/{project_id}/unarchive")
    def unarchive_project(project_id: str, user: dict = Depends(require_auth)):
        """Restore an archived project."""
        # e-1257: owner-only gate via the centralized helper. See archive_project.
        _require_project_role(project_id, user, allowed=("owner",))
        def op(data: dict):
            data["archived"] = False
            return data, {"status": "unarchived", "project_id": project_id}
        return _apply_op_and_broadcast(
            project_id, op, op_name="project.unarchive", actor=user.get("sub", ""),
        )

    @router.post("/api/projects/{project_id}/migrate-to-v2")
    def migrate_project_to_v2(
        project_id: str,
        user: dict = Depends(require_auth),
        _envelope: dict = Depends(
            require_envelope_for_action("project.migrate.v2")
        ),
    ):
        """One-time migration from v1 (whole-doc) to v2 (subcollection) layout.

        Why this exists: an unbounded `milestones[]` array on a single Firestore
        document hits the 1 MiB document size cap. Once over the cap, every
        growth-direction write (task add / log / new milestone) returns 500
        because the resulting doc would exceed 1 MiB. The escape hatch is the
        migration write itself, which moves milestones out to a subcollection
        and shrinks the project doc to ~100 KiB — well under the cap.

        Restricted to project owner (it is destructive in the sense that it
        rewrites the storage layout; owner == only person who should approve).

        Idempotent: a project already at schema_version=2 returns
        {"status": "already_v2"} without doing anything.

        After migration:
          - Reads via `operations.load_project_consistent` hydrate the project
            from meta + subcollection (transparent to callers).
          - Writes via `apply_operation` go through `_apply_cloud_v2` which
            only touches the affected MS subdoc.
          - Writes via `replace_project` (legacy whole-doc PUT) detect v2 and
            dispatch to `_replace_cloud_v2` which decomposes into subdocs.
        """
        # Owner check before kicking off the transaction.
        # e-1257: route through the centralized helper. _require_project_role
        # loads via load_project_consistent which hydrates milestones on v2, but
        # this endpoint is invoked once per project (or returns "already_v2"
        # immediately on v2), so the extra subcollection read is acceptable. The
        # alternative — keeping db.get_project here — would re-fork the auth path
        # and re-create the L687/L730 family of drift that ms-39 exists to close.
        _require_project_role(project_id, user, allowed=("owner",))

        try:
            result = operations.migrate_v1_to_v2(project_id)
        except LookupError:
            # Race: project was deleted between the owner check and the migration.
            raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
        except RuntimeError as e:
            raise HTTPException(status_code=500, detail=str(e))

        return result

    @router.post("/api/projects/{project_id}")
    def create_project(project_id: str, body: ProjectCreate,
                       user: dict = Depends(require_auth)):
        """Create a new project (like beacon init).

        New projects are created with schema_version=2 (β subcollection layout)
        by default — see SPEC doc gP9pCssCoa3QduuSMGR0 §"新規プロジェクトは
        β スキーマで作る (並列性確保)". This lets concurrent writes to different
        milestones proceed without contending on a single document.

        Existing projects (created before this change) remain on schema_version=1
        (legacy whole-document) and are not auto-migrated; apply_operation
        transparently routes them through the legacy transaction path.
        """
        existing = db.get_project(project_id)
        if existing is not None:
            raise HTTPException(status_code=409, detail=f"Project '{project_id}' already exists")
        # ms-95 / e-2794 (2026-07-03): owner を必須化。以前は sub 欠落時に空文字
        # フォールバックで owner="" の project が silent に生まれ、list_projects の
        # migration-period fallthrough と組み合わさって全ユーザー可視の穴を作って
        # いた。deny by default 側の fix と両輪で塞ぐ。
        owner_sub = user.get("sub")
        if not owner_sub:
            raise HTTPException(status_code=401, detail="Authenticated user has no sub claim")
        data = {
            "name": body.name,
            "objective": body.objective,
            "milestones": [],
            "owner": owner_sub,
            "members": [],
            # schema_version: v2 (β subcollection layout) は Firestore 専用
            # (1 MiB / doc cap を避ける設計)。dynamodb / mysql は 1 MiB 制約が無く、
            # v2 経路が Firestore を直呼び (operations.py / _hydrate_v2_milestones)
            # するため非 Firestore backend では動かない。よって非 Firestore では
            # v1 unified で作る (ms-96 e-2379)。
            "schema_version": (
                operations.SCHEMA_V2_BETA
                if os.environ.get("BEACON_STORE_BACKEND", "firestore").lower() == "firestore"
                else 1
            ),
        }
        _save(project_id, data)
        return {"status": "created", "project_id": project_id}

    @router.get("/api/projects/{project_id}")
    def get_project(project_id: str, slim: bool = False,
                    user: dict = Depends(require_auth)):
        # ms-46 e-756: REST もWS pushと同じ enriched shape を返す
        # (total_tasks / done_tasks / entries_to_json)。client がどの経路で
        # データを取っても counts が落ちないように対称化する。
        # ms-84 / e-2326: ?slim=true で entries[] を落とした軽量応答を返す
        # (= Web UI 初期 fetch 用、 entries は MS expand 時に lazy fetch)。
        # default は従来通り full 応答 (= CLI / Tauri IPC 等の既存 consumer 互換)。
        raw = _load(project_id, user)
        return _enrich_project_slim(raw) if slim else _enrich_project(raw)

    @router.get("/api/projects/{project_id}/disclosed-accounts")
    def get_disclosed_accounts(project_id: str, user: dict = Depends(require_auth)):
        """現在のプロジェクト P に開示された、同じ組織の他プロジェクトの顧客(Account)を
        横断して返す (ms-111 / e-3872 = cross-project read, 1 デプロイ内)。

        ms-113 の開示モデルの read 側配線。P に link された Account を「P のメンバー」に
        見せる。判定は各 Account の現在の ``project_links`` を ms-113 の開示プリミティブ
        (``disclosure.can_disclose``) で評価する = 剥奪即時 / fail-closed。

        スコープ (dogfood): 呼び出し user が member である同一 org の他プロジェクトから
        集める (= 自分の営業/開発プロジェクト横断)。user が member でないプロジェクトに
        住む Account を、link 先プロジェクト経由で読む「外部ゲスト cross-read」は本
        endpoint の範囲外 (= 別 authz、follow-up)。ms-111 の cross-instance master store
        は使わない (= 別デプロイ間同期は別途)。
        """
        # 1. lens プロジェクト P へのアクセス権を確認 (P の member でなければ 403/404)。
        #    meta-only で足りる (milestones hydration 不要 = 高頻度経路の負荷を作らない)。
        p_data = _load_meta_only(project_id, user)
        lens_org = org_mod.project_org_id(p_data)
        uid = user.get("sub", "")
        # ms-111 e-3621 chunk2b: identity (会社名 / 担当者) の read を master 経由の
        # resolver に一本化する。server 側は backend 配線済み adapter を持てるので、
        # link 済 Account は master が真値、未 link は投影 fallback (= 従来値・shape 不変)。
        # cross-deploy の master 同期 (別デプロイ間) は本 endpoint の範囲外のまま (e-3622)。
        adapter = master_adapter.get_master_adapter()
        disclosed: list[dict] = []
        # 2. user が member の他プロジェクトを走査し、同一 org のものだけ対象にする。
        for summ in (db.list_projects(uid) or []):
            qid = summ.get("project_id") or summ.get("id")
            if not qid or qid == project_id:
                continue
            q = db.get_project(qid)
            if not q or org_mod.project_org_id(q) != lens_org:
                continue
            # 3. Q の Account のうち P に開示 (project_links に P を含む) されたものだけ。
            for acc in q.get("accounts", []) or []:
                if disclosure_mod.can_disclose(acc, {project_id}):
                    # shape 不変・identity のみ master 経由に (未 link は投影 fallback)。
                    disclosed.append(master_projection.account_read_view(acc, adapter, {
                        "home_project_id": qid,
                        "home_project_name": q.get("name", ""),
                    }))
        return {"project_id": project_id, "disclosed_accounts": disclosed}

    @router.get("/api/projects/{project_id}/milestones/{milestone_id}/entries")
    def get_milestone_entries(project_id: str, milestone_id: str,
                              user: dict = Depends(require_auth)):
        """Return entries[] for a single milestone (ms-84 / e-2326).

        Pair endpoint for the slim WS broadcast: Web UI requests this per-MS
        when the user expands a card. The recursive entry tree is serialized
        via core.entries_to_json so the shape matches the legacy full payload.
        Returns 404 if the milestone is not present in the project.
        """
        raw = _load(project_id, user)
        for ms in raw.get("milestones", []):
            if ms.get("id") == milestone_id:
                entries = ms.get("entries", [])
                return {
                    "milestone_id": milestone_id,
                    "entries": core.entries_to_json(entries),
                }
        raise HTTPException(status_code=404, detail="milestone not found")

    @router.get("/api/projects/{project_id}/pushes")
    def get_project_pushes(project_id: str, user: dict = Depends(require_auth)):
        raw = _load(project_id, user)
        return {"pushes": raw.get("pushes", [])}

    @router.get("/api/projects/{project_id}/deployments")
    def get_project_deployments(project_id: str, user: dict = Depends(require_auth)):
        raw = _load(project_id, user)
        return {"deployments": raw.get("deployments", [])}

    @router.get("/api/projects/{project_id}/worktree-sessions")
    def get_project_worktree_sessions(project_id: str,
                                      user: dict = Depends(require_auth)):
        raw = _load(project_id, user)
        return {"worktree_sessions": raw.get("worktree_sessions", [])}

    @router.put("/api/projects/{project_id}")
    def put_project(project_id: str, body: dict,
                    user: dict = Depends(require_auth)):
        # validate_project is also called inside replace_project, but we pre-call
        # here so the 400 path doesn't open a transaction unnecessarily.
        try:
            core.validate_project(body)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        # Auto-set owner if missing (e.g. cloud push from local)
        if not body.get("owner") and is_auth_enabled():
            body["owner"] = user.get("sub", "")
        # ms-111 e-3621 chunk2b: whole-project write は全 Account が通る唯一の choke point。
        # ここで (flag ON 時のみ) master に link し external_ref を張る。owner 補完後に呼ぶ
        # (org_id 導出が owner に依存するため)。default OFF なので従来挙動は不変。
        _link_body_accounts_to_master(body)
        operations.replace_project(
            project_id, body,
            actor=user.get("sub", ""),
            reason="PUT /api/projects (whole-document replace)",
        )
        # ms-43 / e-2128 — explicit WS broadcast after every write. ms-84 / e-2325:
        # the Firestore on_snapshot listener was disabled (see _start_watcher
        # docstring) because it produced duplicate broadcasts for every write
        # (over-broadcast bug). Single-instance Cloud Run posture makes the
        # explicit broadcast self-sufficient.
        _broadcast_project_after_write(project_id)
        return {"status": "ok", "project_id": project_id}

    @router.post("/api/projects/{project_id}/milestones")
    def create_milestone(project_id: str, body: MilestoneCreate,
                         user: dict = Depends(require_auth)):
        # ms-43 / e-2246 — resolve the human author identity (= user_id / email /
        # display_name) once, then thread it into core.milestone_add so meta.author
        # is stamped at creation time. Mirrors the create_entry contract from
        # ms-78 / e-1909 so MS lists / detail can surface a creator label.
        author = _resolve_author(user)

        def op(data: dict):
            _require_write(data, user)
            try:
                ms_id = core.milestone_add(
                    data, body.title, body.target_date,
                    description=body.description,
                    priority=body.priority,
                    objective=body.objective,
                    acceptance_criteria=body.acceptance_criteria,
                    author=author,
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            return data, {"ms_id": ms_id, "title": body.title}
        return _apply_op_and_broadcast(
            project_id, op, op_name="milestone.create", actor=user.get("sub", ""),
        )

    @router.get("/api/projects/{project_id}/milestones/{ms_id}")
    def get_milestone(project_id: str, ms_id: str,
                      user: dict = Depends(require_auth)):
        data = _load(project_id, user)
        for ms in data["milestones"]:
            if ms["id"] == ms_id:
                entries = ms.get("entries", [])
                total, done = core.count_task_status(entries)
                return {
                    **ms,
                    "total_tasks": total,
                    "done_tasks": done,
                    "entries": core.entries_to_json(entries),
                }
        raise HTTPException(status_code=404, detail=f"Milestone '{ms_id}' not found")

    @router.patch("/api/projects/{project_id}/milestones/{ms_id}")
    def update_milestone(project_id: str, ms_id: str, body: MilestoneUpdate,
                         user: dict = Depends(require_auth)):
        def op(data: dict):
            _require_write(data, user)
            try:
                ms = core.milestone_update(
                    data, ms_id,
                    title=body.title, progress=body.progress,
                    target_date=body.target_date, status=body.status,
                    description=body.description,
                    priority=body.priority or "",  # ms-126: None = no change
                    objective=body.objective,
                    acceptance_criteria=body.acceptance_criteria,
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            return data, {
                "id": ms["id"], "title": work_model.target_label(ms), "status": ms["status"],
                "progress": ms.get("progress", 0),
            }
        return _apply_op_and_broadcast(
            project_id, op, op_name="milestone.update", actor=user.get("sub", ""),
        )

    @router.post("/api/projects/{project_id}/milestones/{ms_id}/start")
    def start_milestone(project_id: str, ms_id: str,
                        user: dict = Depends(require_auth)):
        def op(data: dict):
            _require_write(data, user)
            try:
                ms = core.milestone_start(data, ms_id)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            # ms-109 e-3697 (fable A-3): read the label through the tolerant reader,
            # not ms["title"] directly — the other 5 sites in this file already do.
            # A raw ["title"] would KeyError once the contract step (e-3626) drops
            # the legacy key on canonical-only milestones.
            return data, {"id": ms["id"], "title": work_model.target_label(ms), "status": "in_progress"}
        return _apply_op_and_broadcast(
            project_id, op, op_name="milestone.start", actor=user.get("sub", ""),
        )

    @router.post("/api/projects/{project_id}/milestones/{ms_id}/done")
    def done_milestone(project_id: str, ms_id: str,
                       user: dict = Depends(require_auth)):
        def op(data: dict):
            _require_write(data, user)
            try:
                ms = core.milestone_done(data, ms_id)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            return data, {"id": ms["id"], "title": work_model.target_label(ms), "status": "done"}
        return _apply_op_and_broadcast(
            project_id, op, op_name="milestone.done", actor=user.get("sub", ""),
        )

    @router.delete("/api/projects/{project_id}/milestones/{ms_id}")
    def delete_milestone(project_id: str, ms_id: str,
                         body: Optional[DeleteRequest] = None,
                         user: dict = Depends(require_auth)):
        reason = (body.reason if body else "") or ""
        def op(data: dict):
            _require_write(data, user)
            try:
                ms = core.milestone_delete(data, ms_id, reason=reason)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            return data, {"id": ms["id"], "status": "cancelled"}
        return _apply_op_and_broadcast(
            project_id, op, op_name="milestone.delete", actor=user.get("sub", ""),
        )

    @router.post("/api/projects/{project_id}/milestones/{ms_id}/purge")
    def purge_milestone(
        project_id: str,
        ms_id: str,
        body: PurgeRequest,
        user: dict = Depends(require_auth),
        _envelope: dict = Depends(require_envelope_for_action("milestone.purge")),
    ):
        """Hard-delete a milestone record — owner-only (e-1030).

        Unlike soft delete (`DELETE /milestones/{id}`), this physically removes
        the record from the array (Issue #14 duplicate-ID recovery path). Restricted
        to project owner to protect against accidental destruction by editors.
        """
        if not body.reason:
            raise HTTPException(
                status_code=400,
                detail="reason is required for purge (audit trail per "
                       "data-immutability-principle)",
            )
        def op(data: dict):
            _require_owner(data, user)
            try:
                ms = core.milestone_purge(
                    data, ms_id, reason=body.reason, index=body.index,
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            return data, {
                "id": ms["id"], "title": work_model.target_label(ms), "purged": True,
            }
        return _apply_op_and_broadcast(
            project_id, op, op_name="milestone.purge", actor=user.get("sub", ""),
            reason=body.reason,
        )

    @router.post("/api/projects/{project_id}/milestones/{ms_id}/entries")
    def create_entry(project_id: str, ms_id: str, body: EntryCreate,
                     user: dict = Depends(require_auth)):
        # ms-78 / e-1909 — resolve the human author identity once, then thread it
        # into core.task_add so meta.author is stamped at creation time.
        author = _resolve_author(user)

        def op(data: dict):
            _require_write(data, user)
            try:
                eid = core.task_add(
                    data, ms_id, body.description,
                    entry_type=body.type, date=body.date, detail=body.detail,
                    priority=body.priority,
                    author=author,
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            return data, {"entry_id": eid, "description": body.description}
        return _apply_op_and_broadcast(
            project_id, op, op_name="entry.create", actor=user.get("sub", ""),
        )

    @router.patch("/api/projects/{project_id}/entries/{entry_id}")
    def update_entry(project_id: str, entry_id: str, body: EntryUpdate,
                     user: dict = Depends(require_auth)):
        # ms-78 / e-1909
        author = _resolve_author(user)

        def op(data: dict):
            _require_write(data, user)
            try:
                ms, entry = core.task_update(
                    data, entry_id,
                    description=body.description, status=body.status,
                    detail=body.detail, date=body.date,
                    # ms-126: None (field omitted) = leave priority unchanged; a
                    # provided value is validated by the single-source resolver.
                    priority=body.priority or "",
                    author=author,
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            return data, core.entries_to_json([entry])[0]
        return _apply_op_and_broadcast(
            project_id, op, op_name="entry.update", actor=user.get("sub", ""),
        )

    @router.post("/api/projects/{project_id}/entries/{entry_id}/done")
    def done_entry(project_id: str, entry_id: str, request: Request,
                   user: dict = Depends(require_auth)):
        import datetime
        today = datetime.date.today().isoformat()
        # ms-78 / e-1909
        author = _resolve_author(user)

        def op(data: dict):
            _require_write(data, user)
            try:
                ms, entry = core.task_done(data, entry_id, date=today, author=author)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            return data, {"entry_id": entry_id, "status": "done"}
        result = _apply_op_and_broadcast(
            project_id, op, op_name="entry.done", actor=user.get("sub", ""),
        )
        # ms-88 / e-2167 — mirror task pool done into Trek task_states.
        # Best-effort: failure does not block the task done response.
        try:
            _mirror_task_done_to_treks(entry_id)
        except Exception:
            pass
        # ms-95 / e-2726 — phantom-done evidence gate. Flag only, no reject.
        # Failure is silently swallowed inside the helper; we surface the
        # assessment in the response so the CLI can echo the warning to the
        # operator in the same turn.
        try:
            assessment = _check_phantom_done_evidence(
                project_id, entry_id, user, request=request,
            )
            if (
                isinstance(result, dict)
                and isinstance(assessment, dict)
                and not assessment.get("has_evidence", True)
            ):
                result = {
                    **result,
                    "phantom_done_warning": {
                        "task_id": entry_id,
                        "match_type": assessment.get("match_type", "none"),
                        "commit_window": assessment.get("window", 0),
                        "threshold": assessment.get("threshold"),
                        "message": (
                            "No commit in the recent window references this "
                            "task. Done allowed (= flag, not filter), but the "
                            "lack of physical evidence is logged for audit "
                            "(ms-95 / e-2726)."
                        ),
                    },
                }
        except Exception:
            pass
        return result

    @router.delete("/api/projects/{project_id}/entries/{entry_id}")
    def delete_entry(project_id: str, entry_id: str,
                     body: Optional[DeleteRequest] = None,
                     user: dict = Depends(require_auth)):
        reason = (body.reason if body else "") or ""
        def op(data: dict):
            _require_write(data, user)
            try:
                entry = core.task_delete(data, entry_id, reason=reason)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            return data, {"entry_id": entry_id, "status": "cancelled"}
        return _apply_op_and_broadcast(
            project_id, op, op_name="entry.delete", actor=user.get("sub", ""),
        )

    @router.post("/api/projects/{project_id}/entries/{entry_id}/purge")
    def purge_entry(
        project_id: str,
        entry_id: str,
        body: PurgeRequest,
        user: dict = Depends(require_auth),
        _envelope: dict = Depends(require_envelope_for_action("entry.purge")),
    ):
        """Hard-delete an entry record — owner-only (e-1030).

        Entry-level analogue of milestone purge — Issue #14 / e-863 recovery for
        duplicate entry IDs. Editors cannot purge; only the project owner can.
        """
        if not body.reason:
            raise HTTPException(
                status_code=400,
                detail="reason is required for purge (audit trail per "
                       "data-immutability-principle)",
            )
        def op(data: dict):
            _require_owner(data, user)
            try:
                entry = core.entry_purge(
                    data, entry_id, reason=body.reason, index=body.index,
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            return data, {
                "entry_id": entry.get("id", entry_id),
                "description": entry.get("description", ""),
                "purged": True,
            }
        return _apply_op_and_broadcast(
            project_id, op, op_name="entry.purge", actor=user.get("sub", ""),
            reason=body.reason,
        )

    @router.post("/api/projects/{project_id}/operations/{op_id}/purge")
    def purge_operation(
        project_id: str,
        op_id: str,
        body: PurgeRequest,
        user: dict = Depends(require_auth),
        _envelope: dict = Depends(require_envelope_for_action("operation.purge")),
    ):
        """Hard-delete an operation record — owner-only (e-1030).

        Operation-level analogue of milestone purge — Issue #14 / e-863 recovery
        for duplicate operation IDs. Editors cannot purge; only the project owner
        can.
        """
        if not body.reason:
            raise HTTPException(
                status_code=400,
                detail="reason is required for purge (audit trail per "
                       "data-immutability-principle)",
            )
        def op(data: dict):
            _require_owner(data, user)
            try:
                purged = core.operation_purge(
                    data, op_id, reason=body.reason, index=body.index,
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            return data, {
                "id": purged.get("id", op_id),
                "title": work_model.target_label(purged),
                "purged": True,
            }
        return _apply_op_and_broadcast(
            project_id, op, op_name="operation.purge", actor=user.get("sub", ""),
            reason=body.reason,
        )

    @router.post("/api/projects/{project_id}/operations/{op_id}/envelopes")
    def operation_approve(
        project_id: str,
        op_id: str,
        body: OperationApproveRequest,
        user: dict = Depends(require_auth),
    ):
        """Mint a T2 envelope from a SPEC doc's ``approved_actions``.

        Steps:
          1. Membership check (writer required — minting an authorization is a
             privileged action).
          2. Verify ``op_id`` exists on the project.
          3. Load the SPEC doc, verify it's scope=spec and bound to ``op_id``.
          4. Parse + validate ``approved_actions`` (last-segment wildcards OK
             for T2 per ms-60 SPEC § 設計方針 4).
          5. Issue server-signed envelope via envelope module.
          6. Store record (auto-revoking any prior active envelope for the op).

        Returns the stored envelope record.
        """
        data, _role = _require_project_role(
            project_id, user, allowed=("owner", "editor")
        )
        if not core.find_operations(data, op_id):
            raise HTTPException(
                status_code=404, detail=f"operation not found: {op_id}"
            )
        spec_doc = _spec_doc_for_op(project_id, op_id, body.spec_doc_id)
        content = spec_doc.get("content", "")
        try:
            raw_actions = approved_actions_mod.parse_spec_frontmatter(content)
        except approved_actions_mod.ApprovedActionsError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if raw_actions is None:
            raise HTTPException(
                status_code=400,
                detail=("SPEC doc has no `approved_actions` field in YAML "
                        "frontmatter; nothing to authorize"),
            )
        if not raw_actions:
            raise HTTPException(
                status_code=400,
                detail=("`approved_actions` is empty — an envelope with no "
                        "authorized actions is meaningless"),
            )
        try:
            approved_actions_mod.validate_actions(
                raw_actions, allow_last_segment_wildcard=True
            )
        except approved_actions_mod.ApprovedActionsError as exc:
            raise HTTPException(
                status_code=400, detail=f"invalid approved_actions: {exc}"
            )

        issuer = user.get("email") or user.get("sub") or "dev"
        try:
            envelope_dict = envelope_mod.issue_envelope(
                tier=envelope_mod.TIER_T2,
                issuer=issuer,
                project_id=project_id,
                scope=op_id,
                actions_authorized=raw_actions,
                ttl_seconds=body.ttl_seconds,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        record = db.issue_operation_envelope(
            project_id=project_id,
            op_id=op_id,
            spec_doc_id=body.spec_doc_id,
            spec_revision_id=spec_doc.get("revision_id", ""),
            envelope_dict=envelope_dict,
            approved_actions=raw_actions,
            created_by=issuer,
        )
        return record

    @router.post(
        "/api/projects/{project_id}/operations/{op_id}/envelopes/{envelope_id}/revoke"
    )
    def operation_revoke(
        project_id: str,
        op_id: str,
        envelope_id: str,
        body: OperationRevokeRequest,
        user: dict = Depends(require_auth),
    ):
        """Mark an envelope as revoked. Idempotent.

        ``op_id`` in the URL is verified against the stored record so a typo'd
        URL can't revoke an envelope belonging to a different operation.
        """
        _require_project_role(project_id, user, allowed=("owner", "editor"))
        existing = db.get_operation_envelope(project_id, envelope_id)
        if not existing:
            raise HTTPException(
                status_code=404, detail=f"envelope not found: {envelope_id}"
            )
        if existing.get("op_id") != op_id:
            raise HTTPException(
                status_code=400,
                detail=(f"envelope {envelope_id} belongs to operation "
                        f"{existing.get('op_id')!r}, not {op_id!r}"),
            )
        revoked_by = user.get("email") or user.get("sub") or "dev"
        record = db.revoke_operation_envelope(
            project_id, envelope_id, revoked_by, body.reason or "manual revoke"
        )
        return record

    @router.get("/api/projects/{project_id}/operations/{op_id}/envelopes")
    def operation_envelopes_list(
        project_id: str,
        op_id: str,
        status: Optional[str] = Query(None, description="active | revoked"),
        user: dict = Depends(require_auth),
    ):
        """List envelopes for an operation, newest first.

        ``status`` filter is optional. Read-only members can list (audit visibility).
        """
        _load(project_id, user)  # membership check (any role)
        if status and status not in ("active", "revoked"):
            raise HTTPException(
                status_code=400, detail="status must be 'active' or 'revoked'"
            )
        return db.list_operation_envelopes(project_id, op_id=op_id, status=status)

    @router.post("/api/projects/{project_id}/operation-fires/{op_id}/claim")
    def operation_fire_claim(
        project_id: str,
        op_id: str,
        body: OperationFireClaimRequest,
        user: dict = Depends(require_auth),
    ):
        """Atomically claim "I'm firing op-<id> today" for this project (ms-95).

        First-write-wins per ``(project, op, today)``. Subsequent callers see the
        prior claim and skip their bus push. The CLI scheduler
        (``_auto_fire_operation_triggers`` in lib/commands.py) hits this endpoint
        before posting the operation-trigger bus event so cross-cwd /
        cross-machine parallel bclaude sessions in the same project no longer
        each fire independently (= e-1668 N-multiplied fires / e-2350 4-6 min
        retrigger storms when ``run_record`` lands locally but cloud sync lag
        makes the next scheduler tick still see "no run_record yet").

        Response: ``{claimed: bool, claimed_by: str, claimed_at: str}``. Date is
        server clock (UTC) so all sessions agree on the calendar day boundary
        even across timezone-mixed machines.

        Any project member (= owner / editor / viewer) may claim. The claim
        itself is not a privileged action; the gate exists to dedup honest
        parallel writers, not to enforce access.
        """
        _load(project_id, user)  # membership check (any role)
        import datetime
        today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        return db.claim_operation_fire_if_new(
            project_id, op_id, today, body.session_id or ""
        )

    @router.post("/api/projects/{project_id}/log")
    def log_commit(project_id: str, body: LogCommit,
                   user: dict = Depends(require_auth)):
        # ms-78 / e-1909 — stamp meta.author with the human identity of the
        # signed-in committer (= what the Web UI renders in commit lists).
        author = _resolve_author(user)

        def op(data: dict):
            _require_write(data, user)
            try:
                result = core.log_commit(
                    data, ms_id=body.ms_id, commit_hash=body.hash,
                    message=body.message, date=body.date,
                    summary=body.summary, progress=body.progress,
                    author=author,
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            return data, result
        return _apply_op_and_broadcast(
            project_id, op, op_name="project.log", actor=user.get("sub", ""),
        )

    @router.patch("/api/projects/{project_id}/summary")
    def update_summary(project_id: str, body: SummaryUpdate,
                       user: dict = Depends(require_auth)):
        """**Deprecated** (e-1040 completed). Writes are no-op.

        Cross-session hand-off → `beacon session log` (session_logs subcollection).
        Human narrative → `project-vision` CORE doc.

        The endpoint still returns 200 with the currently-stored summary so
        unknown legacy callers (older CLI / external scripts) don't crash —
        they just observe their input was ignored. The `Deprecation` /
        `Sunset` headers signal the contract change machine-readably.
        """
        # Permission check is still useful (do not leak read access to
        # outsiders), but we don't apply the mutation.
        data = _load(project_id, user)
        _require_write(data, user)
        existing = data.get("summary", "")
        response = JSONResponse(
            content={
                "summary": existing,
                "write_ignored": True,
                "deprecated_since": "e-1040",
            }
        )
        # Standard HTTP deprecation signals.
        response.headers["Deprecation"] = "true"
        response.headers["Sunset"] = "see e-1040; endpoint will be removed"
        response.headers["Link"] = (
            '<https://github.com/kurogin23mech-source/beacon/blob/main/CLAUDE.md>; '
            'rel="deprecation"; type="text/html"'
        )
        return response

    @router.get("/api/projects/{project_id}/documents")
    def list_documents(project_id: str,
                       user: dict = Depends(require_auth)):
        """List all documents for a project."""
        _load(project_id, user)  # access check
        return db.list_documents(project_id)

    @router.get("/api/projects/{project_id}/documents/{doc_id}")
    def get_document(project_id: str, doc_id: str,
                     user: dict = Depends(require_auth)):
        """Get a specific document."""
        _load(project_id, user)  # access check
        doc = db.get_document(project_id, doc_id)
        if doc is None:
            raise HTTPException(status_code=404, detail=f"Document '{doc_id}' not found")
        return doc

    @router.post("/api/projects/{project_id}/documents")
    async def create_document(project_id: str, body: DocumentSave,
                              user: dict = Depends(require_auth)):
        """Create a new document.

        ms-43 e-809: emits a ``document_change`` WS frame after the write so the
        Documents tab on every live client refreshes without waiting for the user
        to re-open the tab. Async only because of that broadcast — the DB write
        itself is sync.
        """
        data = _load(project_id, user)
        _require_write(data, user)
        doc_id = db.save_document(project_id, "", body.title, body.content, body.scope)
        await _broadcast_document_change(
            project_id,
            _build_document_change_payload(project_id, doc_id, op="add",
                                           fallback_title=body.title,
                                           fallback_scope=body.scope),
        )
        return {"doc_id": doc_id, "title": body.title}

    @router.put("/api/projects/{project_id}/documents/{doc_id}")
    async def update_document(project_id: str, doc_id: str, body: DocumentSave,
                              user: dict = Depends(require_auth)):
        """Update an existing document.

        ms-43 e-809: emits a ``document_change`` WS frame post-write so the open
        Documents tab on every client picks up the new title / scope / updated_at
        in-place (instead of staying stale until next tab switch).
        """
        data = _load(project_id, user)
        _require_write(data, user)
        db.save_document(project_id, doc_id, body.title, body.content, body.scope,
                         updated_by=user.get("email", "unknown"))
        await _broadcast_document_change(
            project_id,
            _build_document_change_payload(project_id, doc_id, op="update",
                                           fallback_title=body.title,
                                           fallback_scope=body.scope),
        )
        return {"doc_id": doc_id, "title": body.title}

    @router.get("/api/projects/{project_id}/documents/{doc_id}/revisions")
    def list_document_revisions(project_id: str, doc_id: str, user: dict = Depends(require_auth)):
        """List revision history of a document."""
        _load(project_id, user)
        return db.list_document_revisions(project_id, doc_id)

    @router.get("/api/projects/{project_id}/documents/{doc_id}/revisions/{rev}")
    def get_document_revision(project_id: str, doc_id: str, rev: int, user: dict = Depends(require_auth)):
        """Get a specific revision of a document."""
        _load(project_id, user)
        result = db.get_document_revision(project_id, doc_id, rev)
        if result is None:
            raise HTTPException(status_code=404, detail=f"Revision {rev} not found for '{doc_id}'")
        return result

    @router.delete("/api/projects/{project_id}/documents/{doc_id}")
    async def delete_document_endpoint(project_id: str, doc_id: str,
                                       body: Optional[DeleteRequest] = None,
                                       user: dict = Depends(require_auth)):
        """Soft-delete a document. Optional ``reason`` records why (ms-14 e-991).

        ms-43 e-809: emits a ``document_change`` (op=delete) WS frame so live
        clients drop the entry from their cached ``state.documents`` without
        needing a tab switch to re-fetch. We capture title/scope BEFORE the
        soft-delete because ``list_documents`` filters deleted docs and the
        client may want to render a brief "X was deleted" toast keyed on scope.
        """
        data = _load(project_id, user)
        _require_write(data, user)
        # Snapshot scope/title before delete so the broadcast payload still
        # carries them — once delete_document flips the soft-delete flag,
        # list_documents-style fetches filter the row out, leaving the client
        # without enough context to update its filtered views correctly.
        prior = db.get_document(project_id, doc_id) or {}
        reason = (body.reason if body else "") or ""
        if not db.delete_document(project_id, doc_id,
                                  deleted_by=user.get("email", "unknown"),
                                  reason=reason):
            raise HTTPException(status_code=404, detail=f"Document '{doc_id}' not found")
        payload = {
            "op": "delete",
            "doc_id": doc_id,
            "title": prior.get("title", ""),
            "scope": prior.get("scope", "memo"),
            "updated_at": "",
        }
        milestone = prior.get("milestone")
        if milestone:
            payload["milestone"] = milestone
        await _broadcast_document_change(project_id, payload)
        return {"doc_id": doc_id, "status": "cancelled"}

    @router.post("/api/projects/{project_id}/documents/images")
    async def upload_document_image(project_id: str,
                                    file: UploadFile = File(...),
                                    user: dict = Depends(require_auth)):
        """ms-43: SPEC / memo / retro 本文に貼る画像を 1 枚アップロードする。

        multipart/form-data の ``file`` フィールドにバイナリを乗せて POST する。
        認可は project の write 権限と等価 (= 本文を書ける人なら画像も貼れる)。
        レスポンスは ``{url, markdown}``: ``markdown`` をそのまま doc 本文に
        貼り付けると ``![filename](url)`` として render される。

        保存先と仕様の詳細は ``server/doc_images.py`` 参照 (= GCS bucket、UUID
        key、public read、画像 MIME のみ、10 MiB 上限)。
        """
        data = _load(project_id, user)
        _require_write(data, user)

        contents = await file.read()
        try:
            import doc_images
            result = doc_images.upload_image(
                project_id=project_id,
                filename=file.filename or "image",
                data=contents,
                declared_content_type=file.content_type,
            )
        except ValueError as e:
            # 不正な MIME / サイズ超過 / 空 data 等、client 側に責任がある類。
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            # GCS 接続不能 / bucket 不在 等の server 側障害。
            logger.error("doc image upload failed: %s", e)
            raise HTTPException(status_code=500, detail="image upload failed")

        return {
            "url": result.url,
            "markdown": result.markdown,
            "size": result.size,
            "content_type": result.content_type,
        }

    @router.get("/api/projects/{project_id}/active_claims")
    def list_active_claims_endpoint(project_id: str,
                                    user: dict = Depends(require_auth)):
        """List all active claims on a project, sorted by issued_at."""
        _load(project_id, user)  # access check
        return db.list_active_claims(project_id)

    @router.get("/api/projects/{project_id}/active_claims/{claim_id}")
    def get_active_claim_endpoint(project_id: str, claim_id: str,
                                  user: dict = Depends(require_auth)):
        _load(project_id, user)
        claim = db.get_active_claim(project_id, claim_id)
        if claim is None:
            raise HTTPException(
                status_code=404, detail=f"Claim '{claim_id}' not found",
            )
        return claim

    @router.post("/api/projects/{project_id}/active_claims/{claim_id}")
    def save_active_claim_endpoint(project_id: str, claim_id: str,
                                   body: ActiveClaimSave,
                                   user: dict = Depends(require_auth)):
        """Upsert a claim. Idempotent — same claim_id overwrites."""
        data = _load(project_id, user)
        _require_write(data, user)
        db.save_active_claim(project_id, claim_id, body.payload)
        return {"claim_id": claim_id, "status": "saved"}

    @router.delete("/api/projects/{project_id}/active_claims/{claim_id}")
    def delete_active_claim_endpoint(project_id: str, claim_id: str,
                                     user: dict = Depends(require_auth)):
        """Release a claim from the project-wide store. Idempotent."""
        data = _load(project_id, user)
        _require_write(data, user)
        deleted = db.delete_active_claim(project_id, claim_id)
        return {"claim_id": claim_id, "deleted": deleted}

    @router.get("/api/projects/{project_id}/changelog")
    def list_changelog_endpoint(project_id: str,
                                since: Optional[str] = None,
                                limit: int = 100,
                                user: dict = Depends(require_auth)):
        """Project audit trail — append-only changelog (ms-14 e-825).

        Returns entries newest-first. ``since`` is an ISO8601 timestamp; only
        entries with ``ts > since`` are returned, which makes incremental
        polling cheap. ``limit`` is capped at 500 server-side.
        """
        _load(project_id, user)  # access check
        entries = db.list_changelog(project_id, since=since, limit=limit)
        # ``next_since`` is the oldest ts in this page — pass it back as the
        # cursor for the NEXT older page when the UI scrolls. Empty result
        # means there is nothing further back.
        next_since = entries[-1]["ts"] if entries else None
        return {"entries": entries, "next_since": next_since, "limit": limit}

    return router

# ===========================================================================
# ms-127 e-4871 PR2/3: membership + collaboration/session surface
# (members / invitations / rehome / notes / sessions / session_logs /
#  retros / search). Same pure-move contract as the core slice above.
# ===========================================================================

# ---- PR2 request models (verbatim from app.py) ----

class RetroCreate(BaseModel):
    content: str

class NoteCreate(BaseModel):
    text: str
    context: str = ""
    ts: str = ""
    # ms-57 / e-1036: per-session attribution for the session-log
    # aggregation query. Empty string = "no session" (older clients,
    # pre-ms-57 CLI) and is dropped server-side so Firestore docs stay
    # either "tagged with a real id" or "no field at all".
    session_id: str = ""

class SessionUpsert(BaseModel):
    """Body for PUT /api/projects/{project_id}/sessions/{session_id}.

    Fields mirror lib/session.py's local payload. All optional because heartbeat
    updates only need to bump last_active; first-mint upserts populate the rest.
    server/firestore_client.upsert_session uses merge=True so partial bodies
    are safe.

    ms-54 / e-1318 (Option C true-heartbeat) adds three new fields the bridge
    (channel/bus.mjs) stamps on every poll iteration:

      * ``last_poll_at``     — ISO8601 UTC of the most recent poll iteration.
                               Updated *inside* the bridge's poll loop, so a
                               stale value structurally implies "this bridge
                               cannot receive events" (not "the heartbeat
                               code path ran on a process whose poll loop
                               died long ago").
      * ``poll_interval_ms`` — bridge's poll cadence. Lets the server compute
                               a precise "healthy if last_poll_at within
                               max(30s, 2 × poll_interval_ms)" threshold
                               rather than guessing.
      * ``shutdown``         — True iff the bridge wrote this update as part
                               of a graceful SIGINT/SIGTERM teardown. Used
                               by the directory ``--healthy`` filter to
                               immediately classify deliberately-stopped
                               sessions as not-healthy, instead of waiting
                               for ``last_poll_at`` to go stale.
    """
    actor: Optional[dict] = None
    created_at: Optional[str] = None
    last_active: Optional[str] = None
    harness: Optional[str] = None
    last_poll_at: Optional[str] = None
    poll_interval_ms: Optional[int] = None
    shutdown: Optional[bool] = None

    # ms-54 / e-1369: session transparency in 4 layers (5th is INTENT, written
    # via a separate endpoint so the bridge never mints narrative text).
    #
    #   Layer 0 — Identity     : agent.{kind, version}, harness.{kind, version}
    #   Layer 1 — Where        : cwd, git.{branch, head_short, head_subject}
    #   Layer 2 — What         : focus.{milestone, recent_task}
    #   Layer 3 — Reach        : channels, budget
    #
    # All optional. A bridge that only stamps Identity at mint and Where on
    # heartbeat still serialises correctly via merge=True. The dicts are
    # shaped (rather than flat fields) so adding a sub-key later doesn't bump
    # the SessionUpsert surface area, and the JSON wire format reads like a
    # natural namespace ("agent.version" rather than "agent_version").
    agent: Optional[dict] = None      # {kind, version}
    # NOTE: the legacy top-level `harness: str` above is kept for back-compat;
    # the new structured form lands under runtime.harness instead of replacing
    # the flat field. A bridge can populate both — readers should prefer the
    # nested dict when present.
    runtime: Optional[dict] = None    # {harness: {kind, version}}
    cwd: Optional[str] = None
    git: Optional[dict] = None        # {branch, head_short, head_subject}
    focus: Optional[dict] = None      # {milestone: {id, title}, recent_task: {id, description}}
    channels: Optional[list[str]] = None
    budget: Optional[dict] = None     # {remaining, total}

class SessionIntentUpsert(BaseModel):
    """Body for POST /api/projects/{project_id}/sessions/{session_id}/intent
    (ms-54 / e-1369 Layer 4).

    Intent is the *AI's self-report* of what it is currently doing — the only
    Layer that depends on natural language rather than machine observation.
    The bridge does NOT write intent (it has no insight into the AI's goal);
    the AI stamps it via `beacon session focus "<text>"` or the picker shows
    "(idle)" when absent.

    `attention_required` is a boolean flag the AI raises when it is waiting
    on a human decision. Readers (directory picker, Web UI) show it
    prominently so a teammate sees "who needs me" at a glance.
    """
    text: Optional[str] = None
    attention_required: Optional[bool] = None

class SessionLogUpsert(BaseModel):
    """Body for PUT /api/projects/{project_id}/session_logs/{session_id}.

    ms-57 / e-1037 schema. `summary` is the durable decision-trail content
    (survives entry GC); `*_ids` are best-effort back-references. `recovered`
    is set True only on the first upsert from the rescue path (session-start
    seeing an orphan session) so forensics can tell rescue-born entries from
    session-end ones. All fields optional because rescue and session-end
    write different subsets; firestore_client.upsert_session_log uses
    merge=True so partials are safe.
    """
    summary: Optional[str] = None
    note_ids: Optional[list[str]] = None
    commit_ids: Optional[list[str]] = None
    pr_ids: Optional[list[str]] = None
    created_at: Optional[str] = None
    last_aggregated_at: Optional[str] = None
    recovered: Optional[bool] = None

class MemberInvite(BaseModel):
    email: str
    role: str = "viewer"  # viewer | editor

class MemberRoleUpdate(BaseModel):
    role: str  # viewer | editor

class InvitationCreate(BaseModel):
    email: str
    role: str = "viewer"  # viewer | editor
    expiry_days: int = invitations_mod.DEFAULT_EXPIRY_DAYS

class InvitationAccept(BaseModel):
    display_name: str = ""  # ms-78 e-1807: required-but-allow-server-default

class ProjectRehome(BaseModel):
    org_id: str


# ---- PR2 module-level helpers (invitation-only, self-contained) ----

def _invite_url(token: str) -> str:
    """Build the public landing URL for a token. Honours BEACON_PUBLIC_BASE_URL
    so local dev / staging / prod all produce a clickable link."""
    base = os.environ.get(
        "BEACON_PUBLIC_BASE_URL", "https://beacon-ai.dev"
    ).rstrip("/")
    return f"{base}/join/{token}"

def _resolve_invitation_project(token: str) -> tuple[str, dict, dict]:
    """Resolve (project_id, project_data, invitation_dict) from a plaintext token.

    Tokens carry the project_id as a prefix (= ``<pid>.<random>``) so we can
    look up directly without scanning all projects. Raises 404 on miss.
    """
    pid = invitations_mod.parse_token_project_id(token)
    if not pid:
        raise HTTPException(
            status_code=404,
            detail="Invitation token has no project context. Ask the inviter for a fresh link.",
        )
    data = db.get_project(pid)
    if not data:
        raise HTTPException(
            status_code=404,
            detail="Invitation not found or expired. Ask the inviter for a fresh link.",
        )
    inv = invitations_mod.invitation_find_by_token(data, token)
    if not inv:
        raise HTTPException(
            status_code=404,
            detail="Invitation not found or expired. Ask the inviter for a fresh link.",
        )
    return pid, data, inv


def make_collab_router(
    require_auth: Callable,
    *,
    _load: Callable,
    _load_meta_only: Callable,
    _require_project_role: Callable,
    _require_write: Callable,
    _save: Callable,
    _apply_op_and_broadcast: Callable,
    _load_org_for_member: Callable,
    _session_is_live: Callable,
    _stamp_session_liveness: Callable,
    is_auth_enabled: Callable[[], bool],
) -> APIRouter:
    """Build the /api/projects/* PR2 slice (membership + session/collab surface)
    plus the top-level /api/invitations/{token} accept/preview routes.

    Same injection contract as make_router (the core slice): app.py owns the
    guard family + write/persist/session-liveness helpers and passes them in;
    _compute_poll_health stays in app.py (only its staying _stamp_session_liveness
    caller uses it). Keyword-only + construction-time callability check."""
    for _name, _dep in (
        ("require_auth", require_auth), ("_load", _load),
        ("_load_meta_only", _load_meta_only),
        ("_require_project_role", _require_project_role),
        ("_require_write", _require_write), ("_save", _save),
        ("_apply_op_and_broadcast", _apply_op_and_broadcast),
        ("_load_org_for_member", _load_org_for_member),
        ("_session_is_live", _session_is_live),
        ("_stamp_session_liveness", _stamp_session_liveness),
        ("is_auth_enabled", is_auth_enabled),
    ):
        if not callable(_dep):
            raise TypeError(
                f"routers_projects.make_collab_router: {_name} must be callable, "
                f"got {type(_dep).__name__} — pass a function, not a value."
            )

    router = APIRouter()

    # ---- route handlers (verbatim bodies; @app -> @router) ----

    @router.post("/api/projects/{project_id}/members")
    def invite_member(project_id: str, body: MemberInvite,
                      user: dict = Depends(require_auth)):
        """Invite a member by email. Only project owner can invite."""
        if body.role not in ("viewer", "editor"):
            raise HTTPException(status_code=400, detail="Role must be 'viewer' or 'editor'")
        # Look up the invitee outside the transaction — read-only on the users
        # collection, not the project doc. Safe and avoids extending the txn window.
        found = db.find_user_by_email(body.email)
        if found is None:
            raise HTTPException(
                status_code=404,
                detail=f"User '{body.email}' not found. They must sign in to Beacon first.",
            )
        invited_id, _ = found

        def op(data: dict):
            if is_auth_enabled() and data.get("owner") != user.get("sub"):
                raise HTTPException(
                    status_code=403, detail="Only project owner can invite members"
                )
            members = data.get("members", [])
            if any(m.get("user_id") == invited_id for m in members):
                raise HTTPException(
                    status_code=409, detail=f"'{body.email}' is already a member"
                )
            if data.get("owner") == invited_id:
                raise HTTPException(
                    status_code=409, detail=f"'{body.email}' is the project owner"
                )
            members.append({"user_id": invited_id, "email": body.email, "role": body.role})
            data["members"] = members
            return data, {"status": "invited", "email": body.email, "role": body.role}

        return _apply_op_and_broadcast(
            project_id, op, op_name="member.invite", actor=user.get("sub", ""),
        )

    @router.delete("/api/projects/{project_id}/members/{member_email}")
    def remove_member(project_id: str, member_email: str,
                      user: dict = Depends(require_auth)):
        """Remove a member. Only project owner can remove."""
        def op(data: dict):
            if is_auth_enabled() and data.get("owner") != user.get("sub"):
                raise HTTPException(
                    status_code=403, detail="Only project owner can remove members"
                )
            members = data.get("members", [])
            new_members = [m for m in members if m.get("email") != member_email]
            if len(new_members) == len(members):
                raise HTTPException(
                    status_code=404, detail=f"Member '{member_email}' not found"
                )
            data["members"] = new_members
            return data, {"status": "removed", "email": member_email}

        return _apply_op_and_broadcast(
            project_id, op, op_name="member.remove", actor=user.get("sub", ""),
        )

    @router.get("/api/projects/{project_id}/members")
    def list_members(project_id: str, user: dict = Depends(require_auth)):
        """List project members.

        ms-78 e-1807: enriches each row with the user's `display_name` so the
        UI / CLI can prefer a human-friendly label over the raw email. The field
        is empty when the user hasn't set one yet — the UI should fall back to
        email in that case.
        """
        data = _load(project_id, user)
        owner_id = data.get("owner", "")
        owner_email = ""
        owner_display_name = ""
        if owner_id:
            owner_data = db.get_user(owner_id)
            if owner_data:
                owner_email = owner_data.get("email", "")
                owner_display_name = owner_data.get("display_name", "")
        members = data.get("members", []) or []
        enriched = []
        for m in members:
            if not isinstance(m, dict):
                continue
            m2 = dict(m)
            uid = m.get("user_id", "")
            if uid:
                udata = db.get_user(uid)
                if udata:
                    m2["display_name"] = udata.get("display_name", "") or m2.get(
                        "display_name", ""
                    )
            enriched.append(m2)
        return {
            "owner": owner_id,
            "owner_email": owner_email,
            "owner_display_name": owner_display_name,
            "members": enriched,
        }

    @router.patch("/api/projects/{project_id}/members/{member_email}")
    def update_member_role(project_id: str, member_email: str, body: MemberRoleUpdate,
                           user: dict = Depends(require_auth)):
        """Update a member's role. Only project owner can change roles."""
        if body.role not in ("viewer", "editor"):
            raise HTTPException(status_code=400, detail="Role must be 'viewer' or 'editor'")

        def op(data: dict):
            if is_auth_enabled() and data.get("owner") != user.get("sub"):
                raise HTTPException(
                    status_code=403, detail="Only project owner can change roles"
                )
            members = data.get("members", [])
            for m in members:
                if m.get("email") == member_email:
                    m["role"] = body.role
                    data["members"] = members
                    return data, {"email": member_email, "role": body.role}
            raise HTTPException(
                status_code=404, detail=f"Member '{member_email}' not found"
            )

        return _apply_op_and_broadcast(
            project_id, op, op_name="member.update_role", actor=user.get("sub", ""),
        )

    @router.post("/api/projects/{project_id}/invitations")
    def create_invitation(project_id: str, body: InvitationCreate,
                          user: dict = Depends(require_auth)):
        """Owner issues a fresh invite token. Returns the plaintext token + URL ONCE.

        The plaintext is *never* returned again — if the inviter loses it they
        must cancel + re-issue. The DB only ever sees the SHA256 hash.
        """
        issued: dict = {}

        def op(data: dict):
            if is_auth_enabled() and data.get("owner") != user.get("sub"):
                raise HTTPException(
                    status_code=403,
                    detail="Only project owner can issue invitations",
                )
            try:
                invitation, token = invitations_mod.invitation_create(
                    data,
                    email=body.email,
                    role=body.role,
                    invited_by_user_id=user.get("sub", ""),
                    invited_by_email=user.get("email", ""),
                    expiry_days=body.expiry_days,
                    project_id=project_id,
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            # Stash for outside the txn — the closure may be retried, only the
            # last successful run's values matter.
            issued["invitation"] = invitation
            issued["token"] = token
            return data, None

        _apply_op_and_broadcast(
            project_id, op,
            op_name="invitation.create", actor=user.get("sub", ""),
        )
        invitation = issued.get("invitation") or {}
        token = issued.get("token") or ""
        return {
            "invitation": invitations_mod.invitation_public_view(invitation),
            "token": token,                        # plaintext, returned ONCE
            "url": _invite_url(token),
            "expires_at": invitation.get("expires_at", ""),
            "note": (
                "Beacon project member への招待です。GitHub repo collaborator は別途 "
                "GitHub 側で `gh repo edit --add-collaborator <user>` 等で設定してください。"
            ),
        }

    @router.get("/api/projects/{project_id}/invitations")
    def list_invitations(project_id: str, user: dict = Depends(require_auth)):
        """List active (= unexpired) invitations for a project. Owner-only."""
        data = _load(project_id, user)
        if is_auth_enabled() and data.get("owner") != user.get("sub"):
            raise HTTPException(
                status_code=403,
                detail="Only project owner can view invitations",
            )
        return {
            "invitations": [
                invitations_mod.invitation_public_view(inv)
                for inv in invitations_mod.invitations_list(data)
                if not invitations_mod._is_expired(inv.get("expires_at", ""))
            ],
        }

    @router.delete("/api/projects/{project_id}/invitations/{invitation_id}")
    def cancel_invitation(project_id: str, invitation_id: str,
                          user: dict = Depends(require_auth)):
        """Cancel an outstanding invitation. Owner-only.

        After cancel, the token becomes invalid — even if the invitee still has
        the URL, /accept will return 404.
        """
        def op(data: dict):
            if is_auth_enabled() and data.get("owner") != user.get("sub"):
                raise HTTPException(
                    status_code=403,
                    detail="Only project owner can cancel invitations",
                )
            try:
                removed = invitations_mod.invitation_cancel(data, invitation_id)
            except ValueError as e:
                raise HTTPException(status_code=404, detail=str(e))
            return data, {
                "status": "cancelled",
                "invitation": invitations_mod.invitation_public_view(removed),
            }

        return _apply_op_and_broadcast(
            project_id, op,
            op_name="invitation.cancel", actor=user.get("sub", ""),
        )

    @router.get("/api/invitations/{token}")
    def preview_invitation(token: str):
        """Preview an invitation by plaintext token. Public endpoint — no auth.

        Returns project name / role / inviter so the landing page can render
        "X invited you to Project Y as Z" before the invitee logs in. Does NOT
        return any secrets and does NOT consume the invitation.

        404 if the token does not match any live invitation (= unknown / expired /
        cancelled / already accepted).
        """
        pid, data, inv = _resolve_invitation_project(token)
        owner_id = data.get("owner") or ""
        owner_email = ""
        if owner_id:
            owner_data = db.get_user(owner_id)
            if owner_data:
                owner_email = owner_data.get("email", "")
        return {
            "project_id": pid,
            "project_name": data.get("name", ""),
            "role": inv.get("role", ""),
            "invited_email": inv.get("email", ""),
            "inviter_email": inv.get("invited_by_email", "") or owner_email,
            "expires_at": inv.get("expires_at", ""),
            "owner_email": owner_email,
        }

    @router.post("/api/invitations/{token}/accept")
    def accept_invitation(token: str, body: InvitationAccept,
                          user: dict = Depends(require_auth)):
        """Consume an invite token and add the caller to the project's members.

        Authenticated — the caller must already be signed in (= Google login on
        the landing page). The invitee email must match the email the invitation
        was issued to (= prevents passing the URL to a third party).

        `display_name` is recorded on the user record (= ms-78 e-1807, the
        UC11-F5 "no more raw emails in author columns" goal).

        Idempotent on success in the trivial sense: invitation is consumed and
        the member row is added. A second call returns 404 because the token
        no longer exists.
        """
        target_pid = invitations_mod.parse_token_project_id(token)
        if not target_pid:
            raise HTTPException(
                status_code=404,
                detail="Invitation token has no project context. Ask the inviter for a fresh link.",
            )
        caller_id = user.get("sub", "")
        caller_email = (user.get("email") or "").lower()
        display_name = (body.display_name or "").strip()

        accepted: dict = {}

        def op(data: dict):
            try:
                inv = invitations_mod.invitation_consume(data, token)
            except ValueError as e:
                # Race against another consume / cancel attempt
                raise HTTPException(status_code=404, detail=str(e))
            # Email match check — server enforces, role cannot be re-targeted
            invitee_email = (inv.get("email") or "").lower()
            if caller_email and invitee_email and caller_email != invitee_email:
                # Re-insert the invitation so the legitimate invitee can still use it
                data.setdefault("invitations", []).append(inv)
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"This invitation was issued to {invitee_email}, but you "
                        f"are signed in as {caller_email}. Sign in with the "
                        "invited account and try again."
                    ),
                )
            # Add to project members (= server-side schema, user_id key)
            members = data.setdefault("members", [])
            if not isinstance(members, list):
                members = []
                data["members"] = members
            if data.get("owner") == caller_id:
                # Owner accepting their own invite (= edge case, no-op for membership)
                pass
            elif not any(m.get("user_id") == caller_id for m in members
                         if isinstance(m, dict)):
                members.append({
                    "user_id": caller_id,
                    "email": caller_email,
                    "role": inv.get("role", "viewer"),
                    "joined_at": invitations_mod._now_iso(),
                    "invited_by": inv.get("invited_by", ""),
                })
            accepted["invitation"] = inv
            return data, None

        _apply_op_and_broadcast(
            target_pid, op,
            op_name="invitation.accept", actor=caller_id,
        )

        # Persist display_name on the user record (= UC11-F5 / e-1807).
        # Best-effort — failure here should not block project membership.
        if display_name:
            try:
                db.update_user(caller_id, {"display_name": display_name})
            except Exception:
                pass

        inv = accepted.get("invitation") or {}
        return {
            "status": "accepted",
            "project_id": target_pid,
            "role": inv.get("role", ""),
            "display_name": display_name,
            "next_step_url": f"/?project={target_pid}",
            "note": (
                "Beacon project に追加されました。GitHub repo の collaborator は "
                "別途 GitHub 側で設定が必要です (招待主に依頼してください)。"
            ),
        }

    @router.post("/api/projects/{project_id}/rehome")
    def rehome_project_endpoint(project_id: str, body: ProjectRehome,
                                user: dict = Depends(require_auth)):
        """Re-home a project into a different org — org 所属リンクだけ張り替える (ms-118 / e-4233).

        project の identity (project_id) と履歴は不変で、``org_id`` リンクのみ差し替える
        (SPEC 方針3)。開示は移動後の org 基準で即座に再評価される: ms-113 の開示は
        ``project_org_id`` を request 時に live 参照する (get_disclosed_accounts) ので、
        org_id を書き換えた瞬間から新 org 基準になる (= キャッシュ無し / 剥奪即時、受入条件4)。

        認可 (2 条件の AND):
          - 呼び出し user が project の **owner** であること (= 自分の project しか動かせない。
            editor では不可。破壊的操作の owner-only 統一厳格化は e-4234)。
          - target org が実在し、呼び出し user がその org の **member** であること
            (= 所属していない org に project を吸わせない。非 member / 不在 org は 404 で秘匿)。
        """
        target_org_id = (body.org_id or "").strip()
        if not target_org_id:
            raise HTTPException(status_code=400, detail="org_id is required")
        # target org 実在 + caller が member であることを保証 (非 member / 不在は 404)。
        _load_org_for_member(target_org_id, user)
        # project owner 限定で full doc をロードする (org_id は top-level なので meta-only で足りる)。
        data, _role = _require_project_role(
            project_id, user, allowed=("owner",), hydrate_milestones=False)
        try:
            previous = org_mod.rehome_project(data, target_org_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        _save(project_id, data)
        return {
            "project_id": project_id,
            "org_id": target_org_id,
            "previous_org_id": previous,
        }

    @router.get("/api/projects/{project_id}/notes")
    def list_notes(project_id: str, user: dict = Depends(require_auth)):
        """List session notes from Firestore."""
        _load(project_id, user)
        return db.list_notes(project_id)

    @router.post("/api/projects/{project_id}/notes")
    def add_note(project_id: str, body: NoteCreate, user: dict = Depends(require_auth)):
        """Add a session note."""
        import datetime
        _load(project_id, user)
        note = {
            "ts": body.ts or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "text": body.text,
        }
        if body.context:
            note["context"] = body.context
        if body.session_id:
            note["session_id"] = body.session_id
        note_id = db.add_note(project_id, note)
        return {"note_id": note_id, **note}

    @router.delete("/api/projects/{project_id}/notes")
    def clear_notes(project_id: str, user: dict = Depends(require_auth)):
        """Clear all session notes."""
        data = _load(project_id, user)
        _require_write(data, user)
        db.clear_notes(project_id)
        return {"status": "cleared"}

    @router.put("/api/projects/{project_id}/sessions/{session_id}")
    def upsert_session(
        project_id: str,
        session_id: str,
        body: SessionUpsert,
        user: dict = Depends(require_auth),
    ):
        """Upsert a session registry entry.

        Heartbeat path: CLI sends only `last_active` to refresh liveness without
        overwriting the original mint metadata. Initial mint path: CLI sends the
        full payload. Firestore merge=True (in db.upsert_session) handles both.

        ms-54 / e-1349: the authenticated user's email is stamped onto the
        session's ``actor.email`` field regardless of whether the body included
        actor. Email lives only on the server (bearer token is its property),
        so the bridge cannot fabricate or spoof another user's identity. The
        directory query then surfaces ``actor.email`` so the DM-send Skill
        picker can show a member-level identity ("alice@…") rather than just
        ``machine/agent``. See firestore_client.stamp_session_actor_email for
        the field-path merge that preserves actor.machine/agent in the
        heartbeat (no-actor) path.

        ms-98 (e-3836): this is the heartbeat path (CLI PUTs every few seconds).
        Authorization only needs the project meta doc (owner/members live at the
        top level), and the handler body never reads ``data["milestones"]`` — it
        only writes via ``db.*``. Using ``_load_meta_only`` avoids re-hydrating the
        entire milestones subcollection on every heartbeat, which was a dominant
        source of memory churn in the 2026-07-21 hang incident.
        """
        _load_meta_only(project_id, user)
        payload = {k: v for k, v in body.model_dump().items() if v is not None}
        email = user.get("email", "")
        uid = user.get("sub", "")

        # ms-95: stamp the authenticated user_id (= JWT ``sub``) on the session
        # row so ``_apply_dm_payload_visibility`` can resolve sid→uid without a
        # separate roundtrip. Bridge clients do not (and must not) supply their
        # own user_id — the auth token is the only authority. Without this
        # stamp, ``sid_to_uid[recipient_sid] = ""`` and the visibility gate
        # redacts DM payloads even for the intended recipient (observed 2026-07-06
        # with Iruka / Windows bridge v0.54.0, DM event Y9kG6O6C2Qz5bp2gQ7cN).
        if uid:
            payload["user_id"] = uid

        # Mint path: body carried actor. Stamp email in-line so the single
        # db.upsert_session write below lands the complete view atomically.
        if email and isinstance(payload.get("actor"), dict):
            payload["actor"] = {**payload["actor"], "email": email}

        if not payload and not email:
            # Nothing to write — surface as a no-op rather than a 422, so callers
            # debouncing client-side don't need to special-case empty bodies.
            return {"status": "noop"}
        if payload:
            db.upsert_session(project_id, session_id, payload)
        # Heartbeat path: body had no actor. Stamp the authenticated email via
        # the field-path merge helper so existing actor.machine/agent (from a
        # prior mint) are not stomped. Idempotent — repeat heartbeats just
        # re-write the same email leaf.
        if email and not isinstance(payload.get("actor"), dict):
            db.stamp_session_actor_email(project_id, session_id, email)
        return {"status": "ok", "session_id": session_id}

    @router.get("/api/projects/{project_id}/sessions")
    def list_sessions(
        project_id: str,
        user_id: str = "",
        machine: str = "",
        agent: str = "",
        cwd: str = "",
        agent_kind: str = "",
        live_only: bool = False,
        since_minutes: int = 5,
        healthy_only: bool = False,
        user: dict = Depends(require_auth),
    ):
        """List sessions for a project, with optional directory-query filters.

        ms-54 / e-1134: the rendezvous CLI (e-999) needs to look up "which session
        of this user/machine/agent is currently live" so a sender can pick a DM
        target without knowing the exact session_id out-of-band.

        Filters (all opt-in; the no-arg call still returns everything, ordered by
        last_active desc, to preserve the ms-57 rescue and Web UI 'who is active'
        behavior):

          * ``user_id``       — match ``actor.email`` (user identity surfaces as the
                                email field per session.py's mint convention).
          * ``machine``       — match ``actor.machine`` exactly.
          * ``agent``         — match ``actor.agent`` exactly.
          * ``cwd``           — match the session's ``cwd`` exactly (e-2520 stable
                                recipient identity: part of the coarse key that
                                survives sid re-mint).
          * ``agent_kind``    — match ``agent.kind`` exactly (claude-code / codex).
                                This is the structural agent type, distinct from
                                ``actor.agent`` (a machine label). Together with
                                ``cwd`` + ``machine`` this forms the sid-independent
                                identity a sender can resolve to the current live
                                session.
          * ``live_only``     — drop sessions whose ``last_active`` is older than
                                ``since_minutes`` ago. Heartbeat-based liveness, so
                                a session that crashed without session-end is
                                correctly classified as not-live once its heartbeat
                                goes stale (≥ ms-57 heartbeat cadence + slack).
                                NOTE: ``last_active`` proves only "some heartbeat
                                code path ran"; for "this bridge can actually
                                receive DMs right now" use ``healthy_only``.
          * ``since_minutes`` — threshold for live_only. Default 5 matches the
                                session heartbeat cadence; raise it for "active in
                                last hour" style queries.
          * ``healthy_only``  — e-1318 Option C true-heartbeat. Drop sessions
                                whose bridge poll loop is stale or shutdown. The
                                stale window is ``max(30s, 2× poll_interval_ms)``,
                                so the filter scales with the bridge's own
                                cadence. Sessions without ``last_poll_at`` (never
                                polled — likely an older bridge or no bridge at
                                all) are also dropped under ``healthy_only`` —
                                unknown-liveness is *not* a healthy receiver.

        Every returned row carries a ``poll_health`` block (e-1318) regardless
        of filter choice, so the CLI / Skill consumer can display age & shutdown
        flag in the picker without an extra round-trip.

        Filtering is in-memory after load. The sessions/ subcollection is bounded
        (single-digit to a few dozen docs per project in practice), so we avoid
        Firestore composite-index requirements for what is fundamentally an
        interactive picker query.
        """
        _load(project_id, user)
        sessions = db.list_sessions(project_id)

        import datetime
        now_dt = datetime.datetime.now(datetime.timezone.utc)

        # Always attach poll_health — backward-compat callers that ignore it lose
        # nothing, but /beacon-dm-send (and any other directory consumer) gets
        # the structured signal in one round-trip.
        #
        # Also stamp ``bridge: True/False`` (ms-54 e-1319): True iff a bridge
        # poll loop has ever written ``last_poll_at`` on this session. This is
        # the structural marker for "has a receive channel at all", distinct
        # from poll_health.healthy which factors in age + shutdown. Callers
        # that only want "would a DM have anywhere to land" (e.g. directory
        # default view) can filter on this without re-implementing the
        # last_poll_at presence check.
        for s in sessions:
            _stamp_session_liveness(s, project_id, now_dt)

        if not (user_id or machine or agent or cwd or agent_kind or live_only or healthy_only):
            return sessions

        def _matches(s: dict) -> bool:
            actor = s.get("actor") or {}
            if user_id and actor.get("email", "") != user_id:
                return False
            if machine and actor.get("machine", "") != machine:
                return False
            if agent and actor.get("agent", "") != agent:
                return False
            # e-2520: stable-recipient-identity resolve. cwd + agent_kind are the
            # coarse identity key that survives sid re-mint (bridge restart /
            # daemon churn), so a sender can target "the codex in /path on this
            # machine" instead of a raw ephemeral sid. agent_kind matches the
            # structural agent.kind (claude-code / codex), NOT actor.agent (which
            # is just the machine label). Combined with healthy_only + client-side
            # sort by last_poll_at, this yields the current live sid for a tuple.
            if cwd and (s.get("cwd") or "") != cwd:
                return False
            if agent_kind and ((s.get("agent") or {}).get("kind") or "") != agent_kind:
                return False
            return True

        filtered = [s for s in sessions if _matches(s)]

        if live_only:
            cutoff = now_dt - datetime.timedelta(minutes=since_minutes)
            cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

            # e-3214/e-3220: shared with /api/me/sessions so both directory paths
            # drop shut-down daemons identically (see _session_is_live).
            filtered = [s for s in filtered if _session_is_live(s, cutoff_iso)]

        if healthy_only:
            # ms-101 / e-3010 — 接続ベースの liveness を優先する union 判定に切替。
            # 従来は poll_health.healthy (= last_poll_at 由来) のみを見ていたため、
            # WS では接続中なのに last_poll_at が遅れて healthy=False になる session
            # を取りこぼした (= 「live 0 なのに届く」ズレ)。``live`` は ws_live=True
            # (接続台帳に接続あり) か poll_healthy のどちらかで True になるので、
            # 接続直後の session を即 healthy 受信者として拾える。Redis 不通時は
            # ws_live=None で live == poll_healthy に一致し、従来挙動を保つ。
            filtered = [s for s in filtered if s.get("live") is True]

        return filtered

    @router.put("/api/projects/{project_id}/session_logs/{session_id}")
    def upsert_session_log(
        project_id: str,
        session_id: str,
        body: SessionLogUpsert,
        user: dict = Depends(require_auth),
    ):
        """Upsert a session log entry keyed by session_id (merge=True).

        Both session-end (e-1038) and rescue (e-1039) call this with their own
        subset of fields; merge semantics make the calls commutative — last
        writer wins per field, but no field gets nulled by a partial body.
        """
        _load(project_id, user)
        payload = {k: v for k, v in body.model_dump().items() if v is not None}
        if not payload:
            return {"status": "noop"}
        db.upsert_session_log(project_id, session_id, payload)
        return {"status": "ok", "session_id": session_id}

    @router.get("/api/projects/{project_id}/session_logs/{session_id}")
    def get_session_log(
        project_id: str,
        session_id: str,
        user: dict = Depends(require_auth),
    ):
        """Fetch a single session log entry. Returns 404 if absent."""
        _load(project_id, user)
        doc = db.get_session_log(project_id, session_id)
        if doc is None:
            raise HTTPException(404, detail=f"session_log not found: {session_id}")
        return doc

    @router.get("/api/projects/{project_id}/session_logs")
    def list_session_logs(
        project_id: str,
        limit: int = 0,
        user: dict = Depends(require_auth),
    ):
        """List session logs by last_aggregated_at desc. ``limit=0`` means all."""
        _load(project_id, user)
        return db.list_session_logs(project_id, limit=limit or None)

    @router.post("/api/projects/{project_id}/sessions/{session_id}/intent")
    def upsert_session_intent(
        project_id: str,
        session_id: str,
        body: SessionIntentUpsert,
        user: dict = Depends(require_auth),
    ):
        """Stamp the AI's free-form intent on its own session document.

        Two fields, both optional:

          * ``text``               — 1-line description of what this AI is doing
                                     right now (e.g. "DM read receipt 実装中").
                                     Empty string clears it.
          * ``attention_required`` — True when the session is waiting on a human
                                     decision. The directory picker / Web UI uses
                                     this to surface "who needs me" at a glance.

        The endpoint does NOT enforce that the calling session_id matches the
        authenticated user — multi-agent dispatch may stamp on behalf of a
        sub-session. We do require project membership via _load_meta_only. The
        ``actor.email`` already on the session document is the audit trail for
        who actually owns it.
        """
        # Cost-reduction: intent stamping writes to the session doc only.
        # Membership check needs the meta doc; milestones are never read.
        _load_meta_only(project_id, user)
        payload = body.model_dump(exclude_none=True)
        if not payload:
            return {"status": "noop"}
        # Land under a stable nested key so directory readers know exactly where
        # to look. Avoids the SessionUpsert merge surface entirely.
        db.upsert_session(project_id, session_id, {"intent": payload})
        return {"status": "ok", "session_id": session_id, "intent": payload}

    @router.get("/api/projects/{project_id}/retros")
    def list_retros(project_id: str, user: dict = Depends(require_auth)):
        """List all retrospective documents for a project."""
        return db.list_retros(project_id)

    @router.get("/api/projects/{project_id}/retros/{week}")
    def get_retro(project_id: str, week: str, user: dict = Depends(require_auth)):
        """Get a specific retrospective document."""
        retro = db.get_retro(project_id, week)
        if retro is None:
            raise HTTPException(status_code=404, detail=f"Retro '{week}' not found")
        return retro

    @router.post("/api/projects/{project_id}/retros/{week}")
    def save_retro(project_id: str, week: str, body: RetroCreate,
                   user: dict = Depends(require_auth)):
        """Save a retrospective document."""
        data = _load(project_id, user)
        _require_write(data, user)
        db.save_retro(project_id, week, body.content)
        return {"week": week, "status": "saved"}

    @router.get("/api/projects/{project_id}/search")
    def search_project(
        project_id: str,
        q: str = "",
        type: Optional[str] = None,        # CSV (task,commit,...) — required by CORE doc
        status: Optional[str] = None,      # CSV
        priority: Optional[str] = None,    # CSV
        scope: str = "",
        ms: str = "",
        op: str = "",
        id: str = "",
        assignee: str = "",
        owner: str = "",
        from_: Optional[str] = Query(default=None, alias="from"),
        to: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        user: dict = Depends(require_auth),
    ):
        """Unified search across all Beacon entities.

        See CORE doc 'Beacon 検索基盤の原則' and SPEC '3ne57ccZegYQXDQA03op' for
        the design contract. This endpoint delegates to lib/search.search_project
        so the CLI, server, and Skills all share the same logic.
        """
        import sys as _sys, os as _os
        _LIB = _os.path.join(_os.path.dirname(__file__), "..", "lib")
        if _LIB not in _sys.path:
            _sys.path.insert(0, _LIB)
        import search as _search  # noqa: PLC0415

        data = _load(project_id, user)
        # Hydrate documents from Firestore subcollection.
        documents = db.list_documents(project_id)

        def _split(s: Optional[str]) -> Optional[list[str]]:
            if not s:
                return None
            return [x.strip() for x in s.split(",") if x.strip()]

        return _search.search_project(
            data,
            documents,
            q=q,
            type=_split(type),
            status=_split(status),
            priority=_split(priority),
            scope=scope,
            ms=ms,
            op=op,
            id=id,
            assignee=assignee,
            owner=owner,
            from_date=from_ or "",
            to_date=to or "",
            limit=limit,
            offset=offset,
        )

    return router


def make_bus_gate_router(
    require_auth: Callable,
    *,
    _load: Callable,
    _load_meta_only: Callable,
    _require_project_role: Callable,
) -> APIRouter:
    """Build the /api/projects/* PR3a bus/dm gate slice.

    Same injection contract as make_router / make_collab_router: app.py owns
    the guard family and passes them in as keyword-only callables.
    Keyword-only + construction-time callability check."""
    for _name, _dep in (
        ("require_auth", require_auth), ("_load", _load),
        ("_load_meta_only", _load_meta_only),
        ("_require_project_role", _require_project_role),
    ):
        if not callable(_dep):
            raise TypeError(
                f"routers_projects.make_bus_gate_router: {_name} must be callable, "
                f"got {type(_dep).__name__} — pass a function, not a value."
            )

    router = APIRouter()

    # ---- route handlers (verbatim bodies; @app -> @router) ----

    @router.post("/api/projects/{project_id}/bus/envelope/check-task-add")
    def check_task_add_envelope(
        project_id: str,
        body: CheckTaskAddRequest,
        user: dict = Depends(require_auth),
    ):
        """Decide whether ``task.add`` against ``target_ms`` is auto / propose / reject.

        ms-83 / e-2000. Membership-gated read; the receiver project's
        members are the ones running their AI session through this gate.
        """
        _require_project_role(project_id, user)
        if not body.target_ms:
            raise HTTPException(
                status_code=400,
                detail="target_ms required",
            )

        # Step 1: 9-step verify. Failure → reject (= envelope is broken,
        # don't even propose).
        nonce_store = _get_envelope_nonce_store()
        parent_lookup = _get_envelope_parent_lookup()
        result = envelope_mod.verify(
            body.envelope,
            project_id=project_id,
            payload=body.payload or {},
            requested_action=envelope_mod.TASK_ADD_ACTION,
            nonce_store=nonce_store,
            parent_lookup=parent_lookup,
            sender_session_id="",
        )
        if not result.passed:
            return {
                "permit": "reject",
                "reason": result.rejection_reason or "envelope_verify_failed",
                "steps": result.steps,
            }

        # Step 2: tier-aware scope match.
        tier = body.envelope.get("tier", "")
        if tier == envelope_mod.TIER_T1:
            return {"permit": "auto", "reason": "t1_unrestricted"}
        if tier == envelope_mod.TIER_T2:
            if envelope_mod.check_task_add_scope_match(
                body.envelope, body.target_ms,
            ):
                return {"permit": "auto", "reason": "t2_scope_match"}
            return {
                "permit": "propose",
                "reason": "t2_scope_mismatch_propose_to_ai",
            }
        if tier == envelope_mod.TIER_T1_SYSTEM:
            # T1-system requires a Trek scope walk because the envelope only
            # carries trek:<id>, not the MS list directly.
            scope = body.envelope.get("scope", "") or ""
            if not scope.startswith("trek:"):
                return {
                    "permit": "reject",
                    "reason": "t1_system_scope_malformed",
                }
            trek_id = scope[len("trek:"):]
            trek_doc = db.get_trek(trek_id)
            if trek_doc is None:
                return {
                    "permit": "reject",
                    "reason": "t1_system_trek_not_found",
                }
            if trek_doc.get("status") != "active":
                return {
                    "permit": "reject",
                    "reason": "t1_system_trek_not_active",
                }
            if envelope_mod.trek_scope_includes_ms(trek_doc, body.target_ms):
                return {"permit": "auto", "reason": "t1_system_trek_scope_match"}
            return {
                "permit": "propose",
                "reason": "t1_system_trek_scope_mismatch_propose_to_ai",
            }
        # T3 / T5 / unknown → never auto.
        return {"permit": "propose", "reason": "tier_not_eligible_for_auto"}


    @router.post("/api/projects/{project_id}/bus/envelope/t1-system/issue")
    def issue_t1_system_bus_envelope(
        project_id: str,
        body: T1SystemEnvelopeRequest,
        request: Request,
    ):
        """Issue a T1-system bus envelope (ms-83 / e-1995).

        Authorization: the request MUST present a matching
        ``X-Beacon-Scheduler-Key`` header. This endpoint is the only path
        that mints ``tier=T1-system`` envelopes; receiver-side verify
        cross-checks that ``issuer=beacon-system`` and ``scope=trek:<id>``.

        Validation:
          * The trek must exist and be ``active``. ``planning`` / ``archived``
            Treks cannot have server-mint authority.
          * ``actions_authorized`` is validated by the envelope module
            (strict enumeration, no wildcards).
        """
        # Internal-only authorization. The shared secret is rotated out of
        # band in production; the dev fallback lets the test suite run.
        provided = request.headers.get("X-Beacon-Scheduler-Key", "")
        expected = envelope_mod.scheduler_internal_key()
        if not provided or provided != expected:
            raise HTTPException(
                status_code=403,
                detail="T1-system mint requires X-Beacon-Scheduler-Key",
            )
        # Trek validity gate. The mint is bounded to active Treks so a
        # planning Trek can't accidentally receive auto-execute DMs.
        trek_doc = db.get_trek(body.trek_id)
        if trek_doc is None:
            raise HTTPException(
                status_code=404,
                detail=f"Trek {body.trek_id!r} not found",
            )
        if trek_doc.get("status") != "active":
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Trek {body.trek_id!r} is not active "
                    f"(status={trek_doc.get('status')!r})"
                ),
            )
        try:
            env = envelope_mod.issue_t1_system_envelope(
                project_id=project_id,
                trek_id=body.trek_id,
                actions_authorized=body.actions_authorized,
                data_class=body.data_class,
                ttl_seconds=body.ttl_seconds,
                conversation_id=body.conversation_id,
            )
        except ValueError as e:
            raise HTTPException(
                status_code=400,
                detail=f"T1-system envelope issuance rejected: {e}",
            )
        return env


    @router.post("/api/projects/{project_id}/bus/envelope/issue")
    def issue_bus_envelope(
        project_id: str,
        body: EnvelopeIssueRequest,
        user: dict = Depends(require_auth),
    ):
        """Issue a server-signed bus envelope (e-1155 Phase 1).

        The signature is HMAC-SHA256 over the canonical envelope, keyed by the
        server's ``BEACON_ENVELOPE_SECRET``. The Bearer token on this request
        *is* the proof of human authorization for T1 — the user has explicitly
        asked the server to mint an envelope, which is the structural primitive
        behind "T1 = human explicit signature".

        T2 envelopes (Operation scope) are also minted here. A non-empty
        ``scope`` switches the tier semantics; the envelope module enforces
        the tier/scope consistency rule.

        Rejects wildcards / regex in ``actions_authorized`` at the module
        boundary, so callers cannot smuggle a permissive scope by encoding
        fuzzy intent.
        """
        # Membership check: only writers on this project can mint envelopes
        # against it. Read-only members and unauthenticated callers can't
        # synthesize T1/T2 signatures.
        _require_project_role(project_id, user, allowed=("owner", "editor"))
        issuer = user.get("email") or user.get("sub") or "dev"
        try:
            env = envelope_mod.issue_envelope(
                tier=body.tier,
                issuer=issuer,
                project_id=project_id,
                actions_authorized=body.actions_authorized,
                data_class=body.data_class,
                scope=body.scope,
                conversation_id=body.conversation_id,
                in_reply_to=body.in_reply_to,
                chain_depth=body.chain_depth,
                ttl_seconds=body.ttl_seconds,
                consent_claim=body.recipient_confirmed,
            )
        except ValueError as e:
            raise HTTPException(status_code=400,
                                detail=f"envelope issuance rejected: {e}")
        return env


    @router.get("/api/projects/{project_id}/bus/audit")
    def list_bus_audit(
        project_id: str,
        since: str = "",
        limit: int = 100,
        user: dict = Depends(require_auth),
    ):
        """List bus envelope audit records for ``project_id`` (e-1155 / e-1168).

        Audit visibility is membership-gated: only project members can read.
        """
        _require_project_role(project_id, user)
        return db.list_bus_audit(project_id, since=since, limit=limit)


    @router.get("/api/projects/{project_id}/dm/pending")
    def list_pending_dm_actions(
        project_id: str,
        receiver_user_id: str = "",
        limit: int = 100,
        user: dict = Depends(require_auth),
    ):
        """List pending bus_event_approvals rows (ms-70 / e-1714).

        Used by ``/beacon-session-start`` to surface "保留中の DM action"
        (= cross-user DM bus envelopes that the receiver's terminal was
        closed for when ms-70 / e-1713's dispatcher gate held them).

        The endpoint is membership-gated like other project-scoped reads;
        no extra ACL beyond that because the sidecar's
        ``receiver_user_id`` query gives the caller scoped-to-self filter
        semantics — and read-only members can already see bus events in
        the parent collection.

        ``receiver_user_id`` (optional): restrict to "my pending". Empty
        string returns rows for all receivers in the project (used by web
        UI dashboards / debugging; the Skill always passes a value).

        ms-98 (e-3836): polled by ``/beacon-session-start`` (and Web UI
        dashboards) to surface pending DM actions. Membership-only gate reads the
        meta doc; the handler never touches ``data["milestones"]``, so meta-only
        load avoids the full-project rehydration on each poll.
        """
        _load_meta_only(project_id, user)
        return db.list_pending_approvals(
            project_id,
            receiver_user_id=(receiver_user_id or None),
            limit=limit,
        )


    @router.get("/api/projects/{project_id}/dm/approval/history")
    def list_dm_approval_history(
        project_id: str,
        limit: int = 50,
        user: dict = Depends(require_auth),
    ):
        """List **decided** bus_event_approvals rows for ``project_id`` (ms-70 / e-1718).

        Audit-trail read used by the Web UI's "DM 承認履歴" (DM approval history)
        section, which is read-only by design: SPEC 設計方針 3 keeps every
        approve / deny decision inside the terminal Claude Code that received
        the action, so the Web UI surfaces only the *aftermath* — who decided
        what, when — never an approve / deny control.

        Filters out ``pending`` rows specifically so a future contributor cannot
        casually wire approve / deny buttons on top of this endpoint without
        noticing they would break the terminal-only invariant. ``auto`` rows
        are also excluded — they carry no human decision and would drown out
        the interesting human-decided rows in the audit view.

        Membership-gated like the symmetric ``/dm/pending`` endpoint (e-1714):
        project members can read; non-members cannot. ``decision_by`` is
        returned as the raw user_id stamped by the server at decision time;
        rendering "(you)" suffixes etc. is a presentation concern handled in
        the Web UI.
        """
        _load(project_id, user)
        # Defensive cap. Frontend default is 50; allowing a few hundred is fine
        # for human audit scroll, but unbounded would let a curious client slurp
        # every decision in the project.
        capped = max(1, min(int(limit or 50), 500))
        return db.list_decided_approvals(project_id, limit=capped)


    # ms-70 / e-1716: receiver-side decision endpoint.
    #
    # SPEC 設計方針 3 ("承認は terminal Claude Code 内での user 直接判断のみ") means
    # this endpoint is reached exclusively from `beacon dm respond` typed by the
    # human, never from an autonomous AI loop. The CLI carries the human's Bearer
    # token; the server pulls ``decision_by`` from that token's ``sub`` claim so
    # the CLI cannot spoof "I am someone else" by passing a different user_id.
    #
    # State machine pinned here (idempotency / safety rails):
    #   * sidecar missing (legacy / auto): refuse — there is nothing to decide.
    #   * sidecar pending: write the requested decision_status. Allowed.
    #   * sidecar already approved / denied with the same caller + same decision:
    #       no-op idempotent return (= same user retrying the same press).
    #   * sidecar already approved / denied with a different caller OR different
    #       decision: refuse (= the receiver-of-record already made their call;
    #       a second user or a flip cannot smuggle through this primitive).
    #   * sidecar receiver_user_id != caller's sub: refuse with 403 (= "not your
    #       envelope to decide" — important defense against a curious teammate
    #       clicking a colleague's pending row).
    @router.post("/api/projects/{project_id}/dm/approval/{event_id}")
    def respond_dm_approval(
        project_id: str,
        event_id: str,
        body: DMRespondBody,
        user: dict = Depends(require_auth),
    ):
        """Receiver decides approve / deny on a pending DM-action sidecar (e-1716).

        Returns the resulting 7-field sidecar row, same shape as
        :func:`db.get_bus_event_approval`. The caller's identity (= server-side
        ``user.sub``) is stamped as ``decision_by`` and the server clock is
        stamped as ``decision_at``; both fields are server-authoritative — the
        CLI has no way to pass them in.
        """
        _load(project_id, user)
        # Normalize decision verb. "approve" / "deny" only — no auto / pending
        # flip from this endpoint (auto is set by the dispatcher when blanket-
        # allowing, pending is set when the gate first fires).
        decision = (body.decision or "").strip().lower()
        if decision not in ("approve", "deny"):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"invalid decision {body.decision!r}; "
                    "expected 'approve' or 'deny'"
                ),
            )
        target_status = "approved" if decision == "approve" else "denied"

        # Caller's user_id from auth context. In dev (BEACON_API_AUTH=0) the
        # require_auth dependency returns {'sub': 'dev', 'email': 'dev@local'},
        # so decision_by = "dev" in that mode — consistent with how other
        # actor-stamping endpoints (project.archive etc.) behave in dev.
        caller_uid = user.get("sub") or ""

        existing = db.get_bus_event_approval(project_id, event_id)
        if existing is None:
            # No sidecar = legacy/auto envelope. Nothing to decide.
            raise HTTPException(
                status_code=404,
                detail=(
                    f"no pending approval found for event_id={event_id!r} "
                    f"in project={project_id!r} (the envelope may be legacy / "
                    "auto-allowed, or the event_id is wrong)"
                ),
            )

        receiver_uid = existing.get("receiver_user_id") or ""
        sender_uid = existing.get("sender_user_id") or ""

        # Receiver-of-record check. Only the addressee can decide.
        if caller_uid and receiver_uid and caller_uid != receiver_uid:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"event_id={event_id!r} is addressed to a different user; "
                    "only the intended receiver can approve or deny"
                ),
            )

        current_status = existing.get("approval_status") or ""

        if current_status in ("approved", "denied"):
            # Already decided. Idempotent only when SAME caller chose the SAME
            # outcome — anything else is a structural error.
            if (existing.get("decision_by") == caller_uid
                    and current_status == target_status):
                # Same press, same user: no-op return.
                return existing
            raise HTTPException(
                status_code=409,
                detail=(
                    f"event_id={event_id!r} already decided as "
                    f"{current_status!r} by {existing.get('decision_by')!r} "
                    f"at {existing.get('decision_at')!r}; cannot change to "
                    f"{target_status!r}"
                ),
            )

        if current_status == "auto":
            # Sidecar exists in auto-allowed state (= dispatcher blanket allow).
            # No human decision required; refuse rather than overwriting.
            raise HTTPException(
                status_code=409,
                detail=(
                    f"event_id={event_id!r} is in auto-allowed state and does "
                    "not require an explicit decision"
                ),
            )

        # current_status == "pending" → write the decision. put_bus_event_approval
        # preserves created_at and updates status/decision_by/decision_at.
        now = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        row = db.put_bus_event_approval(
            project_id,
            event_id,
            approval_status=target_status,
            sender_user_id=sender_uid,
            receiver_user_id=receiver_uid,
            decision_by=caller_uid,
            decision_at=now,
        )

        # ms-90 / e-3247: scope 承認/却下も decision-event ストリームに記録する。
        # 記録失敗は決定を壊してはならない (= 付随的) ので握り潰してログするだけ。
        try:
            db.append_decision_event(
                project_id,
                decision_event_mod.decision_event_from_scope_approval(
                    decision=decision,
                    decider_user_id=caller_uid,
                    event_id=event_id,
                    context=body.context or "",
                    rationale=body.rationale or None,
                ),
            )
        except Exception as _dec_exc:  # pragma: no cover - defensive
            logging.getLogger(__name__).warning(
                "append_decision_event (scope-approval) failed for event_id=%s: %s",
                event_id, _dec_exc,
            )

        # ms-70 / e-1717: denied → emit a server-issued T5 reply addressed
        # back to the original envelope's sender_session_id so the AI that
        # tried to act doesn't sit in an infinite-await loop on a bus event
        # whose human gatekeeper just said "no". The approve path needs no
        # such reply because normal delivery resumes once the sidecar flips
        # to approved.
        #
        # T5 chosen per AC 3: this is server-issued, scope=None, no actions,
        # info-disclosure-forbidden (= ping-shape payload only). The CORE doc
        # "高リスク endpoint 一覧" (8iZL1IC92GZ0GwtAUjq5) covers tier escalation
        # paths, not read-only deny notifications — T5 is the right floor here.
        #
        # The payload squeezes into the T5 short-ping schema
        # ({ping/ack/status/kind/ts}, ≤32 chars per string value) by encoding
        # the "denied by receiver" semantics in structural fields:
        #   kind   = "deny"
        #   status = "denied_by_receiver"   (= AC 2 parse anchor)
        #   ack    = "<receiver email or sub>"
        # AC 2 ("body text contains 'denied by receiver' + receiver email")
        # is satisfied structurally: the substring "denied" + "receiver"
        # both appear in status, and the email/sub identifying the receiver
        # is in ack. The literal free-text phrase with a space is not
        # representable in T5 ping shape (= CORE doc "T5 = 短い ping schema");
        # this trade-off is the structural realization of the rule.
        #
        # Failure here is logged + swallowed (warning, not error): the
        # receiver's deny decision is already durably recorded in the sidecar
        # row above; if the reply append fails, that's a downstream-notification
        # gap, not a state-machine corruption. Mirrors e-1713's dispatcher-
        # failure-as-warning policy so a transient Firestore hiccup on the
        # reply path never reverts a human's deny click.
        if decision == "deny":
            try:
                original_event = db.get_bus_event(project_id, event_id)
                if original_event is None:
                    logging.warning(
                        "e-1717: original bus_event %s not found in project %s "
                        "for denied-reply chain; skipping reply append",
                        event_id, project_id,
                    )
                else:
                    sender_session_id = (
                        original_event.get("sender_session_id") or ""
                    )
                    if not sender_session_id:
                        logging.warning(
                            "e-1717: original bus_event %s has no "
                            "sender_session_id; skipping denied-reply chain",
                            event_id,
                        )
                    else:
                        # Receiver identifier — prefer email (= human-readable
                        # in the AI's context), fall back to sub for dev mode.
                        receiver_ident = (
                            user.get("email") or user.get("sub") or ""
                        )
                        # Cap ack at the T5 short-ping value max (32 chars) so
                        # validate_t5_payload accepts it for any plausible email.
                        if len(receiver_ident) > 32:
                            receiver_ident = receiver_ident[:32]

                        # Chain depth: bump from the original envelope so the
                        # 9-step verify's chain_depth ceiling stays honest.
                        original_envelope = (
                            original_event.get("envelope") or {}
                        )
                        parent_chain_depth = (
                            original_envelope.get("chain_depth") or 0
                        )
                        parent_conversation = (
                            original_envelope.get("conversation_id") or None
                        )

                        reply_issuer = (
                            user.get("email") or user.get("sub") or "server"
                        )
                        reply_envelope = envelope_mod.issue_envelope(
                            tier=envelope_mod.TIER_T5,
                            issuer=reply_issuer,
                            project_id=project_id,
                            actions_authorized=[],
                            scope=None,
                            conversation_id=parent_conversation,
                            in_reply_to=event_id,
                            chain_depth=int(parent_chain_depth) + 1,
                        )
                        reply_payload = {
                            "kind": "deny",
                            "status": "denied_by_receiver",
                            "ack": receiver_ident,
                        }
                        reply_data = {
                            "channel": "dm",
                            # The reply is server-issued, not session-issued.
                            # Use an empty sender_session_id sentinel so legacy
                            # readers don't mistake the reply for a human-typed
                            # message; the in_reply_to + payload.kind="deny"
                            # are the canonical signals.
                            "sender_session_id": "",
                            "payload": {
                                **reply_payload,
                                # Routing: receiver-of-original sender's session
                                # is the addressee.
                                "recipient_session_id": sender_session_id,
                                "in_reply_to": event_id,
                            },
                            "delivery": "notify-user-only",
                            "envelope": reply_envelope,
                            "created_at": datetime.datetime.now(
                                datetime.timezone.utc
                            ).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                        }
                        db.append_bus_event(project_id, reply_data)
            except Exception as exc:  # pragma: no cover - defensive
                # Sidecar already records the human's deny — never let a
                # downstream reply-chain hiccup propagate as endpoint failure.
                logging.warning(
                    "e-1717: denied-reply chain append failed for event_id=%s "
                    "in project=%s: %s (deny decision is recorded; sender AI "
                    "will not receive auto-notification this round)",
                    event_id, project_id, exc,
                )

        return row

    return router
