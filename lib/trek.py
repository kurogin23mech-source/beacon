"""Trek model — cross-project / cross-session collaboration area (ms-69 e-1652).

See SPEC doc fnwI2KzzDSJdERIfcwtq (`ms-69 SPEC: Trek — 分散協奏のための作業領域`).

Phase 1 scope (this module):
- Schema constants (valid types / statuses / roles).
- Pure builders / validators for trek docs, members, and scope entries.
- ID minting.

Backend integration (Firestore / DynamoDB CRUD) lives in
``server/firestore_client.py`` and ``server/dynamodb_client.py``. They are
routed through ``server/store_router.py`` exactly like projects / users.

CLI wiring (= ``beacon trek create / show / list / archive`` etc.) is a
follow-up task (e-1653). This module intentionally has no I/O so the
schema can be exercised in unit tests without standing up a DB mock.
"""
from __future__ import annotations

import datetime
import secrets
from typing import Iterable

# Trek lifecycle: planning → active → archived  (3 states only)
# - planning: scope/invites being staged, sessions not yet joining
# - active: members can claim work, DM, run Operations under this trek
# - archived: terminal for temporary treks; persistent treks may archive
#   as "hibernate" and be reactivated later (= caller-enforced)
#
# Halt is **not a status**. The STOP signal sets a separate ``halt`` field
# on the trek doc (= Andon cord); sessions observe it and pause their
# autonomous work. Recovery happens by the leader instructing sessions
# to resume — no state transition required. This collapsed the prior
# planning/active/paused/archived 4-state machine into 3 states because
# pause+resume turned out to be redundant with STOP+leader-instruction.
VALID_TREK_TYPES = ("temporary", "persistent")
VALID_TREK_STATUSES = ("planning", "active", "archived")
VALID_MEMBER_ROLES = ("leader", "member")

DEFAULT_STATUS = "planning"
DEFAULT_TYPE = "persistent"


def mint_trek_id() -> str:
    """Generate a fresh trek id (= 8 hex chars, ~64 bits of entropy).

    Format: ``tk-<8 hex>``. Short enough for CLI legibility, large enough
    to avoid collisions across all treks the system will ever hold.
    """
    return f"tk-{secrets.token_hex(4)}"


def utcnow_iso() -> str:
    """ISO8601 UTC with microseconds + Z suffix (= matches firestore_client)."""
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


def validate_type(t: str) -> str:
    if t not in VALID_TREK_TYPES:
        raise ValueError(
            f"invalid trek type {t!r} — expected one of {VALID_TREK_TYPES}"
        )
    return t


def validate_status(s: str) -> str:
    if s not in VALID_TREK_STATUSES:
        raise ValueError(
            f"invalid trek status {s!r} — expected one of {VALID_TREK_STATUSES}"
        )
    return s


def validate_role(r: str) -> str:
    if r not in VALID_MEMBER_ROLES:
        raise ValueError(
            f"invalid trek role {r!r} — expected one of {VALID_MEMBER_ROLES}"
        )
    return r


def build_actor_ref(*, user_id: str, email: str) -> dict:
    """Canonical actor reference (= user_id + email pair).

    SPEC 設計方針 3 collapses member identity to user grain (user_id + email)
    so a single user with 3 terminals × 2 machines counts as 1 member.
    Leader / claim live at session grain instead (see ``leader_session_id``
    on the trek doc and ms-55 claim model).
    """
    if not user_id or not email:
        raise ValueError("actor ref requires both user_id and email")
    return {"user_id": user_id, "email": email}


def build_halt(*, issued_by_session_id: str, reason: str = "") -> dict:
    """Build a halt record (= the Andon cord signal).

    Set on the trek doc's ``halt`` field by STOP, cleared by resume.
    State stays ``active`` either way — halt is not a status (SPEC 方針 2).
    """
    if not issued_by_session_id:
        raise ValueError("halt requires issued_by_session_id")
    return {
        "issued_at": utcnow_iso(),
        "issued_by_session_id": issued_by_session_id,
        "reason": reason,
    }


def build_member(*, user_id: str, email: str,
                 role: str = "member",
                 invited_at: str | None = None,
                 joined_at: str = "",
                 invited_by: str = "") -> dict:
    """Build a trek member dict.

    ``invited_at`` defaults to now if omitted. ``joined_at`` empty means
    "invited but not yet joined" (= visible to invitee but they have not
    accepted). ``invited_by`` is the user_id of the inviter; empty for
    self-created leader membership at creation time.
    """
    validate_role(role)
    if not user_id or not email:
        raise ValueError("member requires both user_id and email")
    return {
        "user_id": user_id,
        "email": email,
        "role": role,
        "invited_at": invited_at or utcnow_iso(),
        "joined_at": joined_at,
        "invited_by": invited_by,
    }


def normalize_scope_entry(entry: dict) -> dict:
    """Normalise a scope item.

    A scope entry MUST include ``project`` (= project_id) and MAY include
    one of milestone / operation / task to narrow it. Unknown keys are
    dropped to keep the on-disk schema tight (= server side can validate
    against this normalisation, CLI side can also use it before posting).
    """
    if not entry.get("project"):
        raise ValueError("scope entry missing required 'project' field")
    out: dict = {"project": entry["project"]}
    for k in ("milestone", "operation", "task"):
        if entry.get(k):
            out[k] = entry[k]
    return out


DEFAULT_CADENCE_MINUTES = 10
"""ms-83 (= server-side execution continuity / e-1994): default cadence
(= the periodic "next, please" DM interval) in minutes when ``cadence_minutes``
is not set on a trek. 10 minutes balances responsiveness against bus volume.

Stored on ``trek.meta.cadence_minutes`` as an ``int`` (or ``None`` if the
trek operator hasn't set one — the scheduler treats None as the default).
"""


def new_trek(*,
             title: str,
             creator_user_id: str,
             creator_email: str,
             creator_session_id: str,
             description: str = "",
             type_: str = DEFAULT_TYPE,
             initial_scope: Iterable[dict] | None = None,
             goal_state: str = "",
             cadence_minutes: int | None = None,
             manager_agent_url: str = "") -> dict:
    """Build a fresh trek doc (= not yet persisted, no I/O).

    The creator is:
    - recorded as ``creator_actor`` (= user grain, durable identity)
    - automatically added as the first ``member`` with role ``leader``
      (= user grain again, membership is per-person)
    - their session is recorded as ``leader_session_id`` (= the actual
      live session that currently leads the trek, can be transferred
      later via `beacon trek transfer-leader --to <session_id>`)

    Status starts at ``planning`` so the caller can stage scope / invites
    before any session joins. ``halt`` starts None — STOP / resume toggle
    it without changing status (SPEC 方針 2).

    ``goal_state`` (ms-75 / e-1865) is a free-form acceptance criterion
    describing "what completion looks like" for this trek. Optional —
    if empty, the trek's end is decided by the leader's manual archive,
    matching previous behaviour. When non-empty, ``beacon trek show``
    surfaces it so members share a common completion signal, and the
    leader can confidently archive once the criterion is met.

    ``cadence_minutes`` (ms-83 / e-1994) sets how often the server-side
    scheduler (= the loop that fires "next, please" progress-check DMs
    into the trek's claimed session) should wake this trek. ``None`` =
    default 10 minutes (scheduler honours ``DEFAULT_CADENCE_MINUTES``).
    Stored on ``meta`` so the on-disk shape stays orthogonal to
    structural fields (= status / members / scope).

    ``manager_agent_url`` (ms-83 / e-1994) is a schema reservation for
    a future "manager AI" agent endpoint that decides cadence and DM
    body in place of the built-in template. Always optional in this
    MS — the value is recorded but no consumer reads it yet.
    """
    if not title.strip():
        raise ValueError("trek title is required")
    if not creator_session_id:
        raise ValueError(
            "creator_session_id is required (= the session that creates "
            "the trek becomes its initial leader; SPEC 方針 9)"
        )
    validate_type(type_)
    if cadence_minutes is not None:
        _validate_cadence_minutes(cadence_minutes)
    now = utcnow_iso()
    creator_actor = build_actor_ref(
        user_id=creator_user_id, email=creator_email
    )
    leader_member = build_member(
        user_id=creator_user_id, email=creator_email,
        role="leader", invited_at=now, joined_at=now,
        invited_by=creator_user_id,
    )
    scope = [normalize_scope_entry(s) for s in (initial_scope or [])]
    meta: dict = {}
    if cadence_minutes is not None:
        meta["cadence_minutes"] = int(cadence_minutes)
    url = (manager_agent_url or "").strip()
    if url:
        meta["manager_agent_url"] = url
    return {
        "trek_id": mint_trek_id(),
        "title": title.strip(),
        "description": description,
        "type": type_,
        "status": DEFAULT_STATUS,
        "creator_actor": creator_actor,
        "leader_session_id": creator_session_id,
        "members": [leader_member],
        "scope": scope,
        "halt": None,
        "goal_state": (goal_state or "").strip(),
        "meta": meta,
        "created_at": now,
        "updated_at": now,
        "archived_at": None,
    }


def _validate_cadence_minutes(value: int) -> None:
    """Cadence must be a positive int (= no boolean coercion, no zero).

    Zero would burn the bus by firing every server tick; negative makes
    no sense. ms-83 / e-1994.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"cadence_minutes must be int, got {type(value).__name__}"
        )
    if value <= 0:
        raise ValueError(
            f"cadence_minutes must be > 0, got {value}"
        )


def get_cadence_minutes(trek_doc: dict) -> int:
    """Return effective cadence for this trek (= falls back to default).

    Used by the server-side scheduler (= the loop that fires periodic
    progress-check DMs, ms-83 / e-1997). Decoupling the read from the
    field name lets callers stay ignorant of the meta location, and the
    default switch (= ``DEFAULT_CADENCE_MINUTES``) lives in one place.
    """
    meta = trek_doc.get("meta") or {}
    val = meta.get("cadence_minutes")
    if val is None:
        return DEFAULT_CADENCE_MINUTES
    return int(val)


def set_cadence_minutes(trek_doc: dict, *,
                        cadence_minutes: int | None) -> dict:
    """Set or clear ``meta.cadence_minutes`` on an existing trek (ms-83 / e-1994).

    ``None`` clears the field (= scheduler falls back to default).
    Idempotent: re-setting the same value is a no-op so fixtures and
    Skill retries don't churn ``updated_at`` (= mirrors ``set_goal_state``).
    """
    meta = trek_doc.setdefault("meta", {})
    current = meta.get("cadence_minutes")
    if cadence_minutes is None:
        if current is None:
            return trek_doc
        meta.pop("cadence_minutes", None)
    else:
        _validate_cadence_minutes(cadence_minutes)
        if current == int(cadence_minutes):
            return trek_doc
        meta["cadence_minutes"] = int(cadence_minutes)
    trek_doc["updated_at"] = utcnow_iso()
    return trek_doc


def set_manager_agent_url(trek_doc: dict, *,
                          manager_agent_url: str) -> dict:
    """Set or clear ``meta.manager_agent_url`` on an existing trek (ms-83 / e-1994).

    Empty string clears the field. Idempotent: re-setting the same value
    is a no-op. The URL is a **schema reservation** in this MS — no
    consumer reads it yet, so this setter is the only forward edge.
    """
    meta = trek_doc.setdefault("meta", {})
    current = meta.get("manager_agent_url", "")
    new_val = (manager_agent_url or "").strip()
    if new_val == current:
        return trek_doc
    if new_val:
        meta["manager_agent_url"] = new_val
    else:
        meta.pop("manager_agent_url", None)
    trek_doc["updated_at"] = utcnow_iso()
    return trek_doc


def set_goal_state(trek_doc: dict, *, goal_state: str) -> dict:
    """Set or update ``goal_state`` on an existing trek (ms-75 / e-1865).

    Empty string clears the field (= back to "leader decides when done").
    Idempotent: re-setting the same value is a no-op (no updated_at bump)
    so test fixtures and Skill retries don't churn the modification time.
    """
    new_val = (goal_state or "").strip()
    if trek_doc.get("goal_state", "") == new_val:
        return trek_doc
    trek_doc["goal_state"] = new_val
    trek_doc["updated_at"] = utcnow_iso()
    return trek_doc


# Lifecycle transition rules (= server / CLI enforce on state changes).
# 3-state machine: planning → active → archived. ``archived`` is **terminal**
# — to restart work after archive, create a fresh trek with the same scope
# / members. Keeping archived terminal avoids hibernate / wake mode state
# explosion and matches user-facing intuition ("archived = done").
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "planning": frozenset({"active", "archived"}),
    "active": frozenset({"archived"}),
    "archived": frozenset(),  # terminal
}


def validate_transition(from_status: str, to_status: str) -> None:
    """Raise ValueError if ``from_status → to_status`` is not allowed."""
    validate_status(from_status)
    validate_status(to_status)
    if from_status == to_status:
        return
    allowed = ALLOWED_TRANSITIONS.get(from_status, frozenset())
    if to_status not in allowed:
        raise ValueError(
            f"invalid trek transition {from_status!r} → {to_status!r} "
            f"(allowed from {from_status!r}: {sorted(allowed)})"
        )


# ---------------------------------------------------------------------------
# Member operations (ms-69 / e-1654)
#
# Member identity is at user grain (= user_id + email pair), so a single
# user with multiple sessions counts as one member. Per-session presence
# is tracked separately by the session registry (= ``sessions/`` collection)
# and joined at render time, not stored inside the trek doc.
#
# These helpers are pure (= they mutate and return the dict, no I/O).
# Storage callers (lib/trek_store, server/firestore_client) wrap them.
# ---------------------------------------------------------------------------

def find_member(trek_doc: dict, user_id: str) -> dict | None:
    """Return the member dict for ``user_id``, or None if absent."""
    for m in trek_doc.get("members") or []:
        if m.get("user_id") == user_id:
            return m
    return None


def find_member_by_email(trek_doc: dict, email: str) -> dict | None:
    """Return the member dict for ``email``, or None if absent.

    Used by the CLI's local mode invite/join flow where the inviter only
    knows the invitee's email (= cloud user resolution lands in e-1656).
    """
    if not email:
        return None
    for m in trek_doc.get("members") or []:
        if m.get("email") == email:
            return m
    return None


def add_invitation(trek_doc: dict, *,
                   user_id: str, email: str,
                   invited_by_user_id: str) -> dict:
    """Add a new member to the trek with ``joined_at=""`` (= invited, not joined).

    Raises ValueError if the user is already a member. Mutates and returns
    the trek doc so callers can persist with a single save_trek.
    """
    if find_member(trek_doc, user_id) is not None:
        raise ValueError(
            f"user {user_id} is already a member of trek "
            f"{trek_doc.get('trek_id')}"
        )
    new_member = build_member(
        user_id=user_id, email=email,
        role="member",
        invited_at=utcnow_iso(),
        joined_at="",
        invited_by=invited_by_user_id,
    )
    trek_doc.setdefault("members", []).append(new_member)
    trek_doc["updated_at"] = utcnow_iso()
    return trek_doc


def accept_invitation(trek_doc: dict, *, user_id: str) -> dict:
    """Mark a member as joined (= sets ``joined_at`` to now).

    Idempotent: if the member already joined, returns the doc unchanged.
    Raises ValueError if ``user_id`` is not in the members list (= must
    be invited first, no self-add).
    """
    member = find_member(trek_doc, user_id)
    if member is None:
        raise ValueError(
            f"user {user_id} not invited to trek {trek_doc.get('trek_id')} "
            "(owner must `beacon trek invite` first)"
        )
    if member.get("joined_at"):
        return trek_doc  # already joined, no-op
    member["joined_at"] = utcnow_iso()
    trek_doc["updated_at"] = utcnow_iso()
    return trek_doc


def parse_scope_arg(arg: str) -> dict:
    """Parse a CLI ``<project>[:<ref>]`` scope argument into a normalized entry.

    ``ref`` is dispatched by prefix:
    - ``ms-...`` → milestone
    - ``op-...`` → operation
    - ``e-...``  → task
    - omitted    → project-wide scope (= no narrowing)

    Returns the normalized scope dict ready to append to ``trek_doc.scope``.
    Raises ValueError on empty input or unknown ref prefix.
    """
    if not arg or not arg.strip():
        raise ValueError("scope argument cannot be empty")
    arg = arg.strip()
    if ":" in arg:
        project, ref = arg.split(":", 1)
        project = project.strip()
        ref = ref.strip()
        if not project:
            raise ValueError(f"scope arg {arg!r} missing project")
        entry: dict = {"project": project}
        if not ref:
            return normalize_scope_entry(entry)
        if ref.startswith("ms-"):
            entry["milestone"] = ref
        elif ref.startswith("op-"):
            entry["operation"] = ref
        elif ref.startswith("e-"):
            entry["task"] = ref
        else:
            raise ValueError(
                f"unknown ref prefix in {arg!r} — expected ms-/op-/e- "
                "(or omit ref for project-wide scope)"
            )
        return normalize_scope_entry(entry)
    return normalize_scope_entry({"project": arg})


def add_scope_entry(trek_doc: dict, *, entry: dict) -> dict:
    """Append a scope entry; raises ValueError if it already exists."""
    norm = normalize_scope_entry(entry)
    for existing in trek_doc.get("scope") or []:
        if existing == norm:
            raise ValueError(f"scope entry already exists: {norm}")
    trek_doc.setdefault("scope", []).append(norm)
    trek_doc["updated_at"] = utcnow_iso()
    return trek_doc


def remove_scope_entry(trek_doc: dict, *, entry: dict) -> dict:
    """Remove a scope entry; raises ValueError if not found."""
    norm = normalize_scope_entry(entry)
    scope = trek_doc.get("scope") or []
    new_scope = [s for s in scope if s != norm]
    if len(new_scope) == len(scope):
        raise ValueError(f"scope entry not found: {norm}")
    trek_doc["scope"] = new_scope
    trek_doc["updated_at"] = utcnow_iso()
    return trek_doc


# ---------------------------------------------------------------------------
# Halt + leader transfer (ms-69 / e-1662)
#
# Halt = Andon cord. The STOP signal sets the ``halt`` field; participating
# sessions observe it and stop their autonomous work. Status stays
# ``active`` either way (SPEC 方針 2). Resume clears the field.
#
# Leader transfer hands the ``leader_session_id`` from one session to
# another. The current implementation trusts the caller to verify the
# transferring session is the current leader; server-side enforcement
# lands in e-1656 with proper auth.
# ---------------------------------------------------------------------------

def set_halt(trek_doc: dict, *,
             issued_by_session_id: str, reason: str = "") -> dict:
    """Engage the Andon cord. Raises if trek is not currently ``active``.

    Halt is idempotent in the sense that re-issuing replaces the prior
    record (= last STOP wins, more recent ``reason`` survives). Callers
    that need atomicity should check ``trek_doc.get("halt")`` first.
    """
    if trek_doc.get("status") != "active":
        raise ValueError(
            f"can only halt an active trek "
            f"(current status: {trek_doc.get('status')!r})"
        )
    trek_doc["halt"] = build_halt(
        issued_by_session_id=issued_by_session_id, reason=reason,
    )
    trek_doc["updated_at"] = utcnow_iso()
    return trek_doc


def clear_halt(trek_doc: dict) -> dict:
    """Clear the halt signal. Idempotent (= no-op if already cleared)."""
    if not trek_doc.get("halt"):
        return trek_doc
    trek_doc["halt"] = None
    trek_doc["updated_at"] = utcnow_iso()
    return trek_doc


def transfer_leader(trek_doc: dict, *, target_session_id: str) -> dict:
    """Hand off ``leader_session_id`` to another session.

    Caller verifies the requesting session is the current leader (or owner)
    — this helper just performs the swap and stamps updated_at. Server
    auth (e-1656) will enforce the verification later.
    """
    if not target_session_id:
        raise ValueError("target_session_id is required for transfer_leader")
    if trek_doc.get("leader_session_id") == target_session_id:
        return trek_doc  # already the leader, idempotent
    trek_doc["leader_session_id"] = target_session_id
    trek_doc["updated_at"] = utcnow_iso()
    return trek_doc


def remove_member(trek_doc: dict, *, user_id: str) -> dict:
    """Remove a member from the trek.

    Guard rails:
    - Cannot remove the leader (= they must `transfer-leader` first).
    - Cannot remove the last member (= archive the trek instead).
    """
    members = trek_doc.get("members") or []
    target = find_member(trek_doc, user_id)
    if target is None:
        raise ValueError(
            f"user {user_id} not a member of trek {trek_doc.get('trek_id')}"
        )
    if target.get("role") == "leader":
        raise ValueError(
            f"cannot remove leader (user {user_id}); use "
            "`beacon trek transfer-leader` to hand off first"
        )
    new_members = [m for m in members if m.get("user_id") != user_id]
    if not new_members:
        raise ValueError(
            "cannot remove last member; archive the trek instead"
        )
    trek_doc["members"] = new_members
    trek_doc["updated_at"] = utcnow_iso()
    return trek_doc
