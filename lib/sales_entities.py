"""Sales job-template entities — ms-106 ① 営業エンティティ構築.

This is the *sales* profession's ② domain model (see ms-106 SPEC 設計方針 0):
new code, deliberately NOT reusing the milestone/task functions. It lives on
the same shared storage substrate (Store → project.json), which is the L1/③
infrastructure; only the entity shapes and their CRUD are sales-specific.

Entity model (SPEC §0 ER, minimum data layer only — no phase-transition
behaviour, deadline scan, pipeline rollup or UI; those belong to ms-107/108):

  Account       対象・継続 (never closes; keeps a health value)
    └─ contacts[]   sub-entity, nested (SPEC: "Account のフィールドでもいい")
  Opportunity   対象・有限 (closes: won/lost)
    ├─ account_id  参照 association (N:1 → Account, independent lifecycle)
    ├─ phase       exclusive field (a deal is in exactly one phase)
    ├─ phase_history[]  append-only transition log (= 進捗の真値/証拠列,
    │                   the sales analogue of the dev commit series;
    │                   see data-immutability-principle)
    └─ activities[]  従属 composition (N:1 → Opportunity), 業務・事前計画型

ID spaces are sales-local and disjoint from the dev entry space (`e-N`):
Account = ``acc-N`` / Opportunity = ``opp-N`` / Activity = ``act-N``. Because
these collections are unknown keys to ``core.validate_project`` they are not
walked by the dev entry validator, so the ``e-N`` format rule does not apply.

Master / source-of-truth (SPEC §5): in ms-106 the phase state's master is the
human (declares transitions); the source is the meeting (not Beacon-reachable).
Nothing here auto-advances a phase — callers declare it.
"""

from __future__ import annotations

from typing import Optional


# Suggested default phase vocabulary. This is a *reference* ordering for UI /
# templates only — phase is a free string whose master is the human (SPEC §5),
# so callers may declare any phase. Kept here so the template and later
# projections share one list instead of hard-coding it in several places.
DEFAULT_SALES_PHASES = [
    "lead",         # 見込み — first contact / inbound
    "qualified",    # 選別済み — fit confirmed
    "proposal",     # 提案 — proposal sent
    "negotiation",  # 交渉 — terms in discussion
    "closing",      # クロージング — final confirmation
    "won",          # 受注 — closed-won (terminal)
    "lost",         # 失注 — closed-lost (terminal)
]

# Terminal phases close a (有限) Opportunity. Reaching one is goal-terminal;
# kept as data only (no behaviour) so status can mirror it when declared.
TERMINAL_PHASES = {"won", "lost"}

# who_has_the_ball (SPEC §4): whose court the deal is in. Data only in ms-106
# (the deadline scan that acts on it is ms-107's operation adapter).
BALL_SELF = "self"
BALL_COUNTERPART = "counterpart"
VALID_BALL = {BALL_SELF, BALL_COUNTERPART}


# ---------------------------------------------------------------------------
# ID allocation (sales-local counters; max(existing)+1 like the dev allocator
# so physically-removed records don't get their IDs silently re-issued).
# ---------------------------------------------------------------------------

def _next_prefixed_id(ids: list, prefix: str) -> str:
    """Return ``{prefix}{max+1}`` scanning ``{prefix}N`` ids. Non-conforming
    ids are ignored (same tolerance as core.next_op_id)."""
    max_id = 0
    plen = len(prefix)
    for raw in ids:
        if isinstance(raw, str) and raw.startswith(prefix):
            try:
                max_id = max(max_id, int(raw[plen:]))
            except ValueError:
                pass
    return f"{prefix}{max_id + 1}"


def next_account_id(data: dict) -> str:
    return _next_prefixed_id([a.get("id", "") for a in data.get("accounts", [])], "acc-")


def next_opportunity_id(data: dict) -> str:
    return _next_prefixed_id([o.get("id", "") for o in data.get("opportunities", [])], "opp-")


def next_activity_id(data: dict) -> str:
    """Activity ids are global across all opportunities so they are unique
    project-wide (mirrors the dev entry space being flat)."""
    ids = []
    for opp in data.get("opportunities", []):
        ids.extend(act.get("id", "") for act in opp.get("activities", []))
    return _next_prefixed_id(ids, "act-")


# ---------------------------------------------------------------------------
# Finders
# ---------------------------------------------------------------------------

def find_account(data: dict, account_id: str) -> Optional[dict]:
    for a in data.get("accounts", []):
        if a.get("id") == account_id:
            return a
    return None


def find_opportunity(data: dict, opportunity_id: str) -> Optional[dict]:
    for o in data.get("opportunities", []):
        if o.get("id") == opportunity_id:
            return o
    return None


# ---------------------------------------------------------------------------
# Account (対象・継続) + nested Contact
# ---------------------------------------------------------------------------

def account_add(data: dict, name: str, *, health: str = "",
                created_at: str = "") -> str:
    """Append a new Account and return its id.

    ``health`` is the relationship-value slot (SPEC §0 reified 候補, 枠のみ) —
    a free string in ms-106; no computation is attached.
    """
    if not name or not name.strip():
        raise ValueError("Account name is required")
    data.setdefault("accounts", [])
    acc_id = next_account_id(data)
    data["accounts"].append({
        "id": acc_id,
        "name": name.strip(),
        "health": health,
        "contacts": [],
        "created_at": created_at,
    })
    return acc_id


def contact_add(data: dict, account_id: str, name: str, *,
                role: str = "", email: str = "") -> dict:
    """Append a Contact under an Account (nested sub-entity) and return it.

    Contacts are intentionally nested rather than a top-level table: SPEC
    allows "Account のフィールドでもいい". They carry no id in the minimum
    layer (identified by position within the account); promote to a table
    with ``ctc-N`` ids later if a Contact needs to be referenced directly.
    """
    acc = find_account(data, account_id)
    if acc is None:
        raise ValueError(f"Account not found: {account_id}")
    if not name or not name.strip():
        raise ValueError("Contact name is required")
    contact = {"name": name.strip(), "role": role, "email": email}
    acc.setdefault("contacts", []).append(contact)
    return contact


# ---------------------------------------------------------------------------
# Opportunity (対象・有限) — 参照 association → Account
# ---------------------------------------------------------------------------

def opportunity_add(data: dict, title: str, *, account_id: str = "",
                    phase: str = "lead", goal_amount=None, probability=None,
                    deadline: str = "", who_has_the_ball: str = BALL_SELF,
                    created_at: str = "") -> str:
    """Append a new Opportunity and return its id.

    ``account_id`` is a 参照 association (N:1 → Account, independent
    lifecycle). When given it must resolve to an existing Account. The initial
    ``phase`` seeds ``phase_history`` so the evidence series starts non-empty.
    """
    if not title or not title.strip():
        raise ValueError("Opportunity title is required")
    if account_id:
        if find_account(data, account_id) is None:
            raise ValueError(f"Account not found: {account_id}")
    if who_has_the_ball not in VALID_BALL:
        raise ValueError(
            f"who_has_the_ball must be one of {sorted(VALID_BALL)}, got {who_has_the_ball!r}")
    data.setdefault("opportunities", [])
    opp_id = next_opportunity_id(data)
    initial_phase = phase or "lead"
    data["opportunities"].append({
        "id": opp_id,
        "title": title.strip(),
        "account_id": account_id or None,
        "phase": initial_phase,
        "phase_history": [{"phase": initial_phase, "at": created_at, "note": "initial"}],
        "goal_amount": goal_amount,
        "probability": probability,
        "deadline": deadline,
        "who_has_the_ball": who_has_the_ball,
        "status": _status_for_phase(initial_phase),
        "created_at": created_at,
        "activities": [],
    })
    return opp_id


def _status_for_phase(phase: str) -> str:
    """Mirror status from phase. Terminal phases close the (有限) Opportunity.

    Data-only in ms-106: status is a projection of the human-declared phase,
    not an independent state machine.
    """
    if phase == "won":
        return "won"
    if phase == "lost":
        return "lost"
    return "open"


def phase_set(data: dict, opportunity_id: str, new_phase: str, *,
              note: str = "", at: str = "") -> dict:
    """Declare a phase transition (master = human, SPEC §5).

    Appends to ``phase_history`` (append-only 証拠列 — never overwrites) and
    updates the exclusive ``phase`` field + mirrored ``status``. Both the
    current phase and the full traversed history remain queryable (AC12).
    Returns the transition record that was appended.
    """
    opp = find_opportunity(data, opportunity_id)
    if opp is None:
        raise ValueError(f"Opportunity not found: {opportunity_id}")
    if not new_phase or not new_phase.strip():
        raise ValueError("new_phase is required")
    new_phase = new_phase.strip()
    record = {"phase": new_phase, "at": at, "note": note}
    opp.setdefault("phase_history", []).append(record)
    opp["phase"] = new_phase
    opp["status"] = _status_for_phase(new_phase)
    return record


# ---------------------------------------------------------------------------
# Activity (業務・事前計画型) — 従属 composition → Opportunity
# ---------------------------------------------------------------------------

def activity_add(data: dict, opportunity_id: str, description: str, *,
                 deadline: str = "", who_has_the_ball: str = BALL_SELF,
                 created_at: str = "") -> str:
    """Append an Activity under an Opportunity and return its id.

    Activity is the sales 業務 atom (事前計画型: planned first, then driven).
    It is a composition child of exactly one Opportunity (SPEC §3).
    """
    opp = find_opportunity(data, opportunity_id)
    if opp is None:
        raise ValueError(f"Opportunity not found: {opportunity_id}")
    if not description or not description.strip():
        raise ValueError("Activity description is required")
    if who_has_the_ball not in VALID_BALL:
        raise ValueError(
            f"who_has_the_ball must be one of {sorted(VALID_BALL)}, got {who_has_the_ball!r}")
    act_id = next_activity_id(data)
    opp.setdefault("activities", []).append({
        "id": act_id,
        "description": description.strip(),
        "deadline": deadline,
        "status": "todo",
        "who_has_the_ball": who_has_the_ball,
        "created_at": created_at,
    })
    return act_id


# ---------------------------------------------------------------------------
# Sales project template
# ---------------------------------------------------------------------------

def build_sales_project(name: str, objective: str, *, retro_day: str = "monday",
                        disclosure_policy: Optional[dict] = None) -> dict:
    """Return a fresh sales-profession project.json dict.

    Carries an empty ``milestones: []`` purely so the shared
    ``core.validate_project`` (which requires that key) passes unchanged; the
    sales domain lives in ``opportunities`` / ``accounts``. ``profession``
    declares the job-template so ③ shared frames can pick the sales projection.
    """
    data = {
        "name": name,
        "objective": objective,
        "profession": "sales",
        "milestones": [],
        "opportunities": [],
        "accounts": [],
        "retro_day": retro_day,
    }
    if disclosure_policy is not None:
        data["disclosure_policy"] = disclosure_policy
    return data
