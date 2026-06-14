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


def new_trek(*,
             title: str,
             creator_user_id: str,
             creator_email: str,
             creator_session_id: str,
             description: str = "",
             type_: str = DEFAULT_TYPE,
             initial_scope: Iterable[dict] | None = None) -> dict:
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
    """
    if not title.strip():
        raise ValueError("trek title is required")
    if not creator_session_id:
        raise ValueError(
            "creator_session_id is required (= the session that creates "
            "the trek becomes its initial leader; SPEC 方針 9)"
        )
    validate_type(type_)
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
        "created_at": now,
        "updated_at": now,
        "archived_at": None,
    }


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
