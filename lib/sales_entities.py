"""Sales job-template entities — ms-106 ① 営業エンティティ構築.

This is the *sales* profession's ② domain model (see ms-106 SPEC 設計方針 0):
new code, deliberately NOT reusing the milestone/task functions. It lives on
the same shared storage substrate (Store → project.json), which is the L1/③
infrastructure; only the entity shapes and their CRUD are sales-specific.

Entity model (SPEC §0 ER, minimum data layer only — no phase-transition
behaviour, deadline scan, pipeline rollup or UI; those belong to ms-107/108):

  Account       対象・継続 (never closes; keeps a health value + lifecycle phase)
    ├─ phase       exclusive field over the *account* funnel (リード → …)
    └─ contacts[]   sub-entity, nested (SPEC: "Account のフィールドでもいい")
  Opportunity   対象・有限 (closes at a terminal phase: 成約/失注/不成立)
    ├─ account_id  参照 association (N:1 → Account, independent lifecycle)
    ├─ phase       exclusive field over the *opportunity* funnel
    ├─ phase_history[]  append-only transition log (= 進捗の真値/証拠列,
    │                   the sales analogue of the dev commit series)
    └─ activities[]  従属 composition (N:1 → Opportunity), 業務・事前計画型

The phase *funnel itself* is per-company configuration, not a library constant
(案A, 2026-07-13): each target-type carries its own ordered phase vocabulary in
project.json (``account_phases`` / ``opportunity_phases``). This is the essence
of a sales project — the stages + their win-rates ARE that company's sales
methodology (SPEC §6: probability is a manual per-phase parameter). The model
reads the vocabulary from ``data`` rather than hard-coding it.

Master / source-of-truth (SPEC §5): phase transitions are declared by the
human (master). Declaration is permissive — declaring a phase outside the
vocabulary, or a terminal outside a stage's ``allowed_terminals`` rule, yields
a *warning* (via ``phase_warnings``) but is never blocked. The rule lives in
config so it can be tightened later without changing this module.
"""

from __future__ import annotations

from typing import Optional

# --- default funnel seeds (first-user 実フロー, 2026-07-13) -----------------
# Seeded into a fresh sales project.json by build_sales_project. Editable per
# company afterwards (that's the point of storing them as config). These are
# SEEDS, not enforced constants — the live vocabulary is always read from data.

DEFAULT_ACCOUNT_PHASES = [
    {"name": "リード"},          # 未接触 / 見込みのみ
    {"name": "未成約顧客"},      # 商談はあるがまだ成約なし
    {"name": "成約顧客"},        # 成約実績のある継続顧客
]

DEFAULT_OPPORTUNITY_PHASES = [
    # 進行フェーズ: allowed_terminals = ここから宣言できる決着。
    {"name": "商談準備",   "probability": None, "terminal": False, "allowed_terminals": ["不成立"]},
    {"name": "提案準備",   "probability": None, "terminal": False, "allowed_terminals": ["成約", "失注"]},
    {"name": "先方検討中", "probability": None, "terminal": False, "allowed_terminals": ["成約", "失注"]},
    {"name": "合意済み",   "probability": None, "terminal": False, "allowed_terminals": ["成約", "失注"]},
    # 決着フェーズ (terminal): outcome は有限ターゲットの結末種別。
    {"name": "成約",       "probability": 100,  "terminal": True,  "outcome": "won"},
    {"name": "失注",       "probability": 0,    "terminal": True,  "outcome": "lost"},
    {"name": "不成立",     "probability": 0,    "terminal": True,  "outcome": "abandoned"},
]

# who_has_the_ball (SPEC §4): whose court the deal is in. Data only in ms-106.
BALL_SELF = "self"
BALL_COUNTERPART = "counterpart"
VALID_BALL = {BALL_SELF, BALL_COUNTERPART}


# ---------------------------------------------------------------------------
# ID allocation (sales-local counters; max(existing)+1 like the dev allocator).
# ---------------------------------------------------------------------------

def _next_prefixed_id(ids: list, prefix: str) -> str:
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
    ids = []
    for opp in data.get("opportunities", []):
        ids.extend(act.get("id", "") for act in opp.get("activities", []))
    return _next_prefixed_id(ids, "act-")


# ---------------------------------------------------------------------------
# Phase vocabulary (per-company config, read from data)
# ---------------------------------------------------------------------------

def account_phases(data: dict) -> list:
    return data.get("account_phases", [])


def opportunity_phases(data: dict) -> list:
    return data.get("opportunity_phases", [])


def _phase_names(phases: list) -> list:
    return [p.get("name") for p in phases]


def _find_phase_def(phases: list, name: str) -> Optional[dict]:
    for p in phases:
        if p.get("name") == name:
            return p
    return None


def default_account_phase(data: dict) -> str:
    ps = account_phases(data)
    return ps[0]["name"] if ps else DEFAULT_ACCOUNT_PHASES[0]["name"]


def default_opportunity_phase(data: dict) -> str:
    """First non-terminal opportunity phase (= funnel entry). Falls back to
    the first phase, then to the seed's first stage."""
    ps = opportunity_phases(data)
    for p in ps:
        if not p.get("terminal"):
            return p["name"]
    if ps:
        return ps[0]["name"]
    return DEFAULT_OPPORTUNITY_PHASES[0]["name"]


def _opportunity_status_for_phase(data: dict, phase: str) -> str:
    """Mirror status from an opportunity's phase. Terminal phases close the
    (有限) Opportunity, projecting to their configured outcome."""
    pdef = _find_phase_def(opportunity_phases(data), phase)
    if pdef and pdef.get("terminal"):
        return pdef.get("outcome") or "closed"
    return "open"


def opportunity_phase_warnings(data: dict, current_phase: str, new_phase: str) -> list:
    """Non-blocking checks for an opportunity phase transition (SPEC §5:
    master=人間 declares; we surface, never block). Returns warning strings:

    * new_phase not in the configured vocabulary, and
    * declaring a terminal that the current stage's ``allowed_terminals`` rule
      does not permit (e.g. 商談準備 → 成約, which skips 提案).
    """
    warnings: list = []
    phases = opportunity_phases(data)
    if not phases:
        return warnings  # no vocabulary configured → nothing to check against
    names = _phase_names(phases)
    if new_phase not in names:
        warnings.append(
            f"'{new_phase}' は opportunity_phases の語彙にありません "
            f"(既知: {', '.join(names)})")
        return warnings
    new_def = _find_phase_def(phases, new_phase)
    cur_def = _find_phase_def(phases, current_phase)
    if new_def and new_def.get("terminal") and cur_def is not None:
        allowed = cur_def.get("allowed_terminals")
        if allowed is not None and new_phase not in allowed:
            warnings.append(
                f"'{current_phase}' から決着できるのは {allowed} のみです "
                f"('{new_phase}' はルール外)")
    return warnings


def account_phase_warnings(data: dict, new_phase: str) -> list:
    warnings: list = []
    phases = account_phases(data)
    if not phases:
        return warnings
    names = _phase_names(phases)
    if new_phase not in names:
        warnings.append(
            f"'{new_phase}' は account_phases の語彙にありません "
            f"(既知: {', '.join(names)})")
    return warnings


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

def account_add(data: dict, name: str, *, health: str = "", phase: str = "",
                created_at: str = "") -> str:
    """Append a new Account (対象・継続) and return its id.

    Account carries a lifecycle ``phase`` (リード → 未成約顧客 → 成約顧客) with
    an append-only ``phase_history``, plus the ``health`` relationship-value
    slot (SPEC §0 reified 候補, 枠のみ). It never reaches a terminal (継続).
    """
    if not name or not name.strip():
        raise ValueError("Account name is required")
    data.setdefault("accounts", [])
    acc_id = next_account_id(data)
    initial_phase = phase or default_account_phase(data)
    data["accounts"].append({
        "id": acc_id,
        "name": name.strip(),
        "phase": initial_phase,
        "phase_history": [{"phase": initial_phase, "at": created_at, "note": "initial"}],
        "health": health,
        "contacts": [],
        "created_at": created_at,
    })
    return acc_id


def contact_add(data: dict, account_id: str, name: str, *,
                role: str = "", email: str = "") -> dict:
    """Append a Contact under an Account (nested sub-entity) and return it."""
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
                    phase: str = "", goal_amount=None, probability=None,
                    deadline: str = "", who_has_the_ball: str = BALL_SELF,
                    created_at: str = "") -> str:
    """Append a new Opportunity (対象・有限) and return its id.

    ``account_id`` is a 参照 association (N:1 → Account) validated when given.
    ``phase`` defaults to the configured funnel entry and seeds phase_history.
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
    initial_phase = phase or default_opportunity_phase(data)
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
        "status": _opportunity_status_for_phase(data, initial_phase),
        "created_at": created_at,
        "activities": [],
    })
    return opp_id


def phase_set(data: dict, target_id: str, new_phase: str, *,
              note: str = "", at: str = "") -> dict:
    """Declare a phase transition on an Opportunity (``opp-``) or Account
    (``acc-``), dispatched by id prefix (master = human, SPEC §5).

    Appends to the target's append-only ``phase_history`` and updates its
    exclusive ``phase`` (+ mirrored ``status`` for opportunities). Returns the
    appended transition record. This is permissive: use
    ``opportunity_phase_warnings`` / ``account_phase_warnings`` to surface
    vocabulary / terminal-rule violations to the caller.
    """
    if not new_phase or not new_phase.strip():
        raise ValueError("new_phase is required")
    new_phase = new_phase.strip()
    if target_id.startswith("opp-"):
        opp = find_opportunity(data, target_id)
        if opp is None:
            raise ValueError(f"Opportunity not found: {target_id}")
        record = {"phase": new_phase, "at": at, "note": note}
        opp.setdefault("phase_history", []).append(record)
        opp["phase"] = new_phase
        opp["status"] = _opportunity_status_for_phase(data, new_phase)
        return record
    if target_id.startswith("acc-"):
        acc = find_account(data, target_id)
        if acc is None:
            raise ValueError(f"Account not found: {target_id}")
        record = {"phase": new_phase, "at": at, "note": note}
        acc.setdefault("phase_history", []).append(record)
        acc["phase"] = new_phase
        return record
    raise ValueError(
        f"target id must start with 'opp-' or 'acc-', got {target_id!r}")


# ---------------------------------------------------------------------------
# Activity (業務・事前計画型) — 従属 composition → Opportunity
# ---------------------------------------------------------------------------

def activity_add(data: dict, opportunity_id: str, description: str, *,
                 deadline: str = "", who_has_the_ball: str = BALL_SELF,
                 created_at: str = "") -> str:
    """Append an Activity (業務・事前計画型) under an Opportunity, return its id."""
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
# Deletion (referential integrity: 参照 association is checked; composition
# children are removed with their parent).
# ---------------------------------------------------------------------------

def account_delete(data: dict, account_id: str, *, force: bool = False) -> list:
    """Remove an Account. Returns the list of opportunity ids that referenced
    it (orphaned when ``force``).

    Because Opportunity → Account is a 参照 association (independent lifecycle),
    deleting a referenced Account is refused unless ``force`` — with ``force``
    the referencing opportunities are orphaned (``account_id`` set to None)
    rather than cascade-deleted (a lost deal shouldn't vanish with its account).
    """
    acc = find_account(data, account_id)
    if acc is None:
        raise ValueError(f"Account not found: {account_id}")
    referencing = [o["id"] for o in data.get("opportunities", [])
                   if o.get("account_id") == account_id]
    if referencing and not force:
        raise ValueError(
            f"Account {account_id} is referenced by {referencing}; reassign "
            f"those opportunities or pass force=True to orphan them")
    if referencing and force:
        for o in data.get("opportunities", []):
            if o.get("account_id") == account_id:
                o["account_id"] = None
    data["accounts"] = [a for a in data.get("accounts", [])
                        if a.get("id") != account_id]
    return referencing


def opportunity_delete(data: dict, opportunity_id: str) -> None:
    """Remove an Opportunity and its composition children (activities go with
    it — they have no life independent of the deal)."""
    if find_opportunity(data, opportunity_id) is None:
        raise ValueError(f"Opportunity not found: {opportunity_id}")
    data["opportunities"] = [o for o in data.get("opportunities", [])
                             if o.get("id") != opportunity_id]


# ---------------------------------------------------------------------------
# Sales project template
# ---------------------------------------------------------------------------

def build_sales_project(name: str, objective: str, *, retro_day: str = "monday",
                        disclosure_policy: Optional[dict] = None) -> dict:
    """Return a fresh sales-profession project.json dict.

    Seeds the per-company phase funnels (``account_phases`` /
    ``opportunity_phases``) with the first-user default; these are editable
    config, not constants. Carries an empty ``milestones: []`` so the shared
    ``core.validate_project`` passes unchanged.
    """
    data = {
        "name": name,
        "objective": objective,
        "profession": "sales",
        "milestones": [],
        "opportunities": [],
        "accounts": [],
        "account_phases": [dict(p) for p in DEFAULT_ACCOUNT_PHASES],
        "opportunity_phases": [dict(p) for p in DEFAULT_OPPORTUNITY_PHASES],
        "retro_day": retro_day,
    }
    if disclosure_policy is not None:
        data["disclosure_policy"] = disclosure_policy
    return data
