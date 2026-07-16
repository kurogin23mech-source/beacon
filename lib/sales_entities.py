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

import work_base

# --- default funnel seeds (first-user 実フロー, 2026-07-13) -----------------
# Seeded into a fresh sales project.json by build_sales_project. Editable per
# company afterwards (that's the point of storing them as config). These are
# SEEDS, not enforced constants — the live vocabulary is always read from data.

DEFAULT_ACCOUNT_PHASES = [
    {"name": "リード"},          # 未接触 / 見込みのみ
    {"name": "未成約顧客"},      # 商談はあるがまだ成約なし
    {"name": "成約顧客"},        # 成約実績のある継続顧客
]

# 各進行フェーズは面談で区切られる (商談準備→初回面談→提案準備→提案面談→先方
# 検討中→先方合意→合意済み、最低2回・同期合意なら3回の面談)。methodology
# (goal / activity_template / transition_signal / default_lead) は ms-107 e-3375
# でユーザー実務から確定した基本4フェーズの型。transition_signal の "calendar_ended"
# は面談実施を、"manual" は人間判定を意味する (= SIGNAL_* 定数と同値)。default_lead
# は面談未定時のフォールバック日数 (本命の遷移日は実際の面談日 = カレンダー由来)。
# 詳細見積の提出は「提案段階の時も検討段階の時もある」状況依存のためテンプレに
# 入れず、AI の文脈生成 (e-3373) が必要な時だけ足す。これらは SEED (編集可能な
# 出発点) であり enforce される定数ではない。
DEFAULT_OPPORTUNITY_PHASES = [
    # 進行フェーズ: allowed_terminals = ここから宣言できる決着。
    {"name": "商談準備", "probability": 10, "terminal": False,
     "allowed_terminals": ["不成立"],
     "goal": "初回面談の実施により、商談として進行可能な状態にする",
     "activity_template": ["初回面談を打診", "初回面談を実施", "提案の方向性を確定"],
     "transition_signal": "calendar_ended", "default_lead": 7},
    {"name": "提案準備", "probability": 20, "terminal": False,
     "allowed_terminals": ["成約", "失注"],
     "goal": "企画を作り提案を終え、先方が検討フェーズに入った状態にする",
     "activity_template": ["提案面談を打診", "提案面談を実施", "提案内容を準備"],
     "transition_signal": "calendar_ended", "default_lead": 14},
    {"name": "先方検討中", "probability": 40, "terminal": False,
     "allowed_terminals": ["成約", "失注"],
     "goal": "先方の実行合意を取る",
     "activity_template": ["合意確認日を確定（必要なら面談設定）", "合意の確認を取る"],
     "transition_signal": "manual", "default_lead": 14},
    {"name": "合意済み", "probability": 80, "terminal": False,
     "allowed_terminals": ["成約", "失注"],
     "goal": "契約を締結する",
     "activity_template": ["契約書を送付", "締結"],
     "transition_signal": "manual", "default_lead": 7},
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
# ID allocation. The ``max(existing)+1`` algorithm now lives in the shared
# ``work_base.next_suffixed_id`` (ms-109 e-3558) — the same one the development
# allocator uses — instead of being re-implemented here. Only the sales-specific
# part (which id list to hand it, per prefix) stays below.
# ---------------------------------------------------------------------------

def _next_prefixed_id(ids: list, prefix: str) -> str:
    return work_base.next_suffixed_id(ids, prefix)


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
# Phase methodology (config schema) — ms-107 e-3371, SPEC §1/§7.
# ---------------------------------------------------------------------------
# A phase definition (a dict in a target-type's phase vocabulary) may carry the
# *methodology* of that phase, on top of the ms-106 fields (name / probability /
# terminal / allowed_terminals / outcome):
#
#   goal              str   — このフェーズが達成したいゴール (1 行)
#   activity_template list  — ゴールへ向かう期待活動のテンプレ (固定でなく「期待」,
#                             SPEC §4: 面談 outcome で reconcile する)
#   transition_signal str   — 遷移の判定手段 (どう判定するか, 下記 SIGNAL_*)
#   on_fail           dict  — 判定が失敗した時の分岐設定 (下記 on_fail schema)
#   default_lead      int   — フェーズ入場時に遷移日を置く既定リード日数
#
# These are the *target-class generic* vocabulary (SPEC §7: engine core は
# "商談" を語に焼き込まない). The accessors below read a phase_def dict without
# knowing whether the target is an opportunity — so ms-109 can drive the same
# engine over milestones/tasks by supplying a different phase vocabulary.
#
# All fields are OPTIONAL: the ms-106 seed does not carry them yet (the 営業
# アダプタ の初期 methodology は e-3375 で seed する). Accessors default
# gracefully so live projects created before this task keep working.

PHASE_METHODOLOGY_FIELDS = (
    "goal", "activity_template", "transition_signal", "on_fail", "default_lead")

# transition_signal vocabulary (SPEC §5: 決定的〜判断的 スペクトラム). The
# detection wiring lands in later tasks (calendar auto-detect = e-3374); here we
# only fix the vocabulary so config can name a signal and the engine can branch.
SIGNAL_MANUAL = "manual"                    # 人間が遷移を宣言する (既定)
SIGNAL_CALENDAR_ACCEPTED = "calendar_accepted"  # 相手が招待を accept → 面談確定
SIGNAL_CALENDAR_ENDED = "calendar_ended"    # event.end < now → 面談実施済み
SIGNAL_COUNTERPART_REPLY = "counterpart_reply"  # 先方からの返信 (メール等)
VALID_TRANSITION_SIGNALS = {
    SIGNAL_MANUAL, SIGNAL_CALENDAR_ACCEPTED, SIGNAL_CALENDAR_ENDED,
    SIGNAL_COUNTERPART_REPLY,
}


def phase_goal(phase_def: Optional[dict]) -> str:
    """The phase's goal string, or '' when unset."""
    return (phase_def or {}).get("goal", "") or ""


def phase_activity_template(phase_def: Optional[dict]) -> list:
    """The phase's expected-activity template (list), or [] when unset.

    SPEC §4: this is an *expectation*, not a fixed pipeline — the engine
    reconciles it against real meeting outcomes rather than enforcing it.
    """
    tpl = (phase_def or {}).get("activity_template")
    return list(tpl) if tpl else []


def phase_transition_signal(phase_def: Optional[dict]) -> str:
    """How this phase's transition is judged. Defaults to SIGNAL_MANUAL — with
    no signal configured the human declares the transition (master, SPEC §6)."""
    return (phase_def or {}).get("transition_signal") or SIGNAL_MANUAL


def phase_on_fail(phase_def: Optional[dict]) -> Optional[dict]:
    """The failure-branch config for this phase, or None.

    Shape (all optional): ``{"terminals": [<phase-name>...], "retry": bool}``.
    ``terminals`` narrows the決着 choices on failure (falls back to the phase's
    ``allowed_terminals`` when absent); ``retry`` marks that staying in-phase
    with a new transition_date is allowed (SPEC §3: advance / retry / terminal).
    The 3-way judgement itself is human-confirmed and lives in e-3372.
    """
    of = (phase_def or {}).get("on_fail")
    return of if isinstance(of, dict) else None


def phase_default_lead(phase_def: Optional[dict]) -> Optional[int]:
    """Default lead time (in days) for placing this phase's transition_date on
    entry, or None when unset. Kept as a plain int-of-days; date arithmetic is
    the caller's job (the engine, e-3372)."""
    lead = (phase_def or {}).get("default_lead")
    if lead is None:
        return None
    try:
        return int(lead)
    except (TypeError, ValueError):
        return None


def phase_methodology(phase_def: Optional[dict]) -> dict:
    """Normalized methodology view of a phase_def (target-class generic).

    Always returns every PHASE_METHODOLOGY_FIELDS key with a graceful default,
    so the engine and callers never KeyError on a phase that predates this
    schema (or on the ms-106 seed, which does not carry methodology yet).
    """
    return {
        "goal": phase_goal(phase_def),
        "activity_template": phase_activity_template(phase_def),
        "transition_signal": phase_transition_signal(phase_def),
        "on_fail": phase_on_fail(phase_def),
        "default_lead": phase_default_lead(phase_def),
    }


def opportunity_phase_methodology(data: dict, phase_name: str) -> dict:
    """Methodology for a named opportunity phase (looked up in the configured
    vocabulary). Returns the same defaulted shape as ``phase_methodology`` even
    when the phase name is unknown, so callers get a stable dict."""
    pdef = _find_phase_def(opportunity_phases(data), phase_name)
    return phase_methodology(pdef)


def opportunity_phase_is_terminal(data: dict, phase_name: str) -> bool:
    """True when the named opportunity phase is a 決着 (terminal) stage."""
    pdef = _find_phase_def(opportunity_phases(data), phase_name)
    return bool(pdef and pdef.get("terminal"))


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
                assignee: str = "", created_at: str = "") -> str:
    """Append a new Account (対象・継続) and return its id.

    Account carries a lifecycle ``phase`` (リード → 未成約顧客 → 成約顧客) with
    an append-only ``phase_history``, plus the ``health`` relationship-value
    slot (SPEC §0 reified 候補, 枠のみ). It never reaches a terminal (継続).

    ``assignee`` (担当ユーザー) is a target-class-generic slot mirroring
    Milestone.assignee (ms-81); it names the project member who owns this
    account. ``nurturings`` は 商談 (Opportunity) を持たない継続関係に対する
    ナーチャリング業務 (業務・事前計画型) を Account に直接ぶら下げる入れ物
    (ms-106 fb3、Opportunity.activities の継続 target 版)。
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
        "assignee": assignee,
        "contacts": [],
        "nurturings": [],
        "created_at": created_at,
    })
    return acc_id


def contact_add(data: dict, account_id: str, name: str, *,
                role: str = "", email: str = "", phone: str = "") -> dict:
    """Append a Contact under an Account (nested sub-entity) and return it."""
    acc = find_account(data, account_id)
    if acc is None:
        raise ValueError(f"Account not found: {account_id}")
    if not name or not name.strip():
        raise ValueError("Contact name is required")
    contact = {"name": name.strip(), "role": role, "email": email, "phone": phone}
    acc.setdefault("contacts", []).append(contact)
    return contact


def account_rename(data: dict, account_id: str, new_name: str) -> dict:
    """Rename an Account (対象・継続). Returns the mutated account. The name is
    a plain label (not history-tracked like phase), so this is an in-place edit."""
    acc = find_account(data, account_id)
    if acc is None:
        raise ValueError(f"Account not found: {account_id}")
    if not new_name or not new_name.strip():
        raise ValueError("Account name is required")
    acc["name"] = new_name.strip()
    return acc


def set_assignee(data: dict, target_id: str, assignee: str) -> dict:
    """Set the 担当ユーザー (assignee) on an Opportunity (``opp-``) or Account
    (``acc-``), dispatched by id prefix. Target-class-generic mutation that
    mirrors Milestone.assignee (ms-81); ms-109 folds all three into one engine.

    ``assignee`` is a free string (project member id / name); "" clears it.
    Returns the mutated target.
    """
    if target_id.startswith("opp-"):
        target = find_opportunity(data, target_id)
    elif target_id.startswith("acc-"):
        target = find_account(data, target_id)
    else:
        raise ValueError(
            f"target id must start with 'opp-' or 'acc-', got {target_id!r}")
    if target is None:
        raise ValueError(f"Target not found: {target_id}")
    target["assignee"] = assignee
    return target


# ---------------------------------------------------------------------------
# Opportunity (対象・有限) — 参照 association → Account
# ---------------------------------------------------------------------------

def opportunity_add(data: dict, title: str, *, account_id: str = "",
                    phase: str = "", goal_amount=None, probability=None,
                    deadline: str = "", who_has_the_ball: str = BALL_SELF,
                    transition_date: str = "", assignee: str = "",
                    created_at: str = "") -> str:
    """Append a new Opportunity (対象・有限) and return its id.

    ``account_id`` is a 参照 association (N:1 → Account) validated when given.
    ``phase`` defaults to the configured funnel entry and seeds phase_history.
    ``transition_date`` (= 遷移日, SPEC §2) is the planned date on which this
    phase's goal is judged; optional at creation (the engine prompts to place
    it when the target enters a non-terminal phase without one).
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
        "assignee": assignee,
        "transition_date": transition_date or "",
        "transition_date_history": (
            [{"transition_date": transition_date, "at": created_at, "note": "initial"}]
            if transition_date else []),
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
        # ms-106 fb3 / e-3350 — project the linked Account's lifecycle phase from
        # its opportunities' furthest progress. Advance UP only (a customer who
        # once closed stays 成約顧客); humans can still override via acc- phase_set.
        if opp.get("account_id"):
            _auto_advance_account_phase(data, opp["account_id"], at=at)
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
# Account phase projection (顧客フェーズ ← 商談フェーズ, ms-106 fb3 / e-3350)
# ---------------------------------------------------------------------------
# The account lifecycle (リード → 未成約顧客 → 成約顧客) is NOT independent state:
# it is a *rollup* of the furthest progress across the account's opportunities.
# This is sales-adapter-specific (the vocabulary + mapping are 営業固有); the
# generic form ("継続 target の phase を子の有限 target 群の rollup で更新する")
# is what ms-109 hoists into the target-class engine.

def _opp_progress_signals(data: dict, opp: dict) -> tuple:
    """Return ``(won, lost, max_nonterminal_idx)`` an opportunity ever reached,
    scanning its ``phase_history`` + current phase. ``max_nonterminal_idx`` is
    the furthest funnel position (0-based among non-terminal phases), -1 if
    none. 不成立 (abandoned) counts as neither won nor lost — the seed only
    allows it from the 商談準備 entry, so it is not "progress"."""
    phases = opportunity_phases(data)
    nonterm = [p.get("name") for p in phases if not p.get("terminal")]
    won = lost = False
    max_nt = -1
    names = [e.get("phase") for e in opp.get("phase_history", [])]
    if opp.get("phase"):
        names.append(opp.get("phase"))
    for name in names:
        pdef = _find_phase_def(phases, name)
        if pdef and pdef.get("terminal"):
            outcome = pdef.get("outcome")
            if outcome == "won":
                won = True
            elif outcome == "lost":
                lost = True
        elif name in nonterm:
            max_nt = max(max_nt, nonterm.index(name))
    return won, lost, max_nt


def _account_phase_idx(data: dict, phase_name: str) -> int:
    aps = account_phases(data) or DEFAULT_ACCOUNT_PHASES
    for i, p in enumerate(aps):
        if p.get("name") == phase_name:
            return i
    return -1


def derive_account_phase(data: dict, account_id: str) -> Optional[str]:
    """Project an Account's lifecycle phase from its opportunities' furthest
    progress. Mapping is by *position* in ``account_phases`` so it stays
    config-generic (works even if a company renames its stages):

      idx 0 (リード):     商談なし / 全商談が商談準備以下 (未進行)
      idx 1 (未成約顧客): 提案準備以降へ進んだ or 失注した商談が1つ以上、成約なし
      idx 2 (成約顧客):   成約 (won) 商談が1つ以上

    Returns the account phase NAME (clamped to the configured vocabulary), or
    ``None`` when no account phases are configured.
    """
    aps = account_phases(data) or DEFAULT_ACCOUNT_PHASES
    if not aps:
        return None
    won = lost = progressed = False
    for o in data.get("opportunities", []):
        if o.get("account_id") != account_id:
            continue
        w, l, max_nt = _opp_progress_signals(data, o)
        won = won or w
        lost = lost or l
        if max_nt >= 1:
            progressed = True
    idx = 2 if won else (1 if (lost or progressed) else 0)
    idx = min(idx, len(aps) - 1)
    return aps[idx].get("name")


def _auto_advance_account_phase(data: dict, account_id: str, *, at: str = "") -> Optional[dict]:
    """Advance the account's stored phase to its derived value, but only UP the
    funnel (never auto-downgrade). Records the transition in ``phase_history``
    with an auto-note so the projection stays auditable. Returns the appended
    record, or ``None`` if no advance happened."""
    acc = find_account(data, account_id)
    if acc is None:
        return None
    derived = derive_account_phase(data, account_id)
    if not derived:
        return None
    cur_idx = _account_phase_idx(data, acc.get("phase"))
    new_idx = _account_phase_idx(data, derived)
    if new_idx > cur_idx:
        record = {"phase": derived, "at": at,
                  "note": "商談の進行により自動昇格 (derive_account_phase)"}
        acc.setdefault("phase_history", []).append(record)
        acc["phase"] = derived
        return record
    return None


# ---------------------------------------------------------------------------
# Pipeline value & member targets (見込み売上 / メンバー別 目標売上, ms-106 fb4)
# ---------------------------------------------------------------------------
# 見込み売上 (weighted pipeline) = Σ over OPEN (non-terminal) opportunities of
# goal_amount × phase.probability. Member targets (目標売上) live in a *sparse*
# project-level map ``sales_targets`` {member: amount}: a member added later
# simply has no entry (= 目標未設定) until one is set — additive, no migration.
# This is L3 (sales-adapter) config; ms-109 generalizes it to "a target carries
# a quota / KPI".

def set_opportunity_amount(data: dict, opp_id: str, amount) -> dict:
    """Set an opportunity's goal_amount (商談金額). ``amount`` is a number (円)
    or None to clear. Returns the mutated opportunity."""
    opp = find_opportunity(data, opp_id)
    if opp is None:
        raise ValueError(f"Opportunity not found: {opp_id}")
    opp["goal_amount"] = amount
    return opp


def set_phase_probability(data: dict, phase_name: str, probability) -> dict:
    """Set a per-company win probability (成約率, 0-100) on an opportunity phase
    definition. Config-level edit (per-company funnel tuning). Returns the def."""
    pdef = _find_phase_def(opportunity_phases(data), phase_name)
    if pdef is None:
        raise ValueError(f"Opportunity phase not found: {phase_name}")
    pdef["probability"] = probability
    return pdef


def sales_targets(data: dict) -> dict:
    return data.get("sales_targets", {})


def set_sales_target(data: dict, member: str, amount) -> dict:
    """Set a member's 目標売上 (sales quota) in the sparse ``sales_targets`` map.
    ``amount`` None removes the entry (= 目標未設定 に戻す). Returns the map."""
    if not member or not str(member).strip():
        raise ValueError("member is required")
    key = str(member).strip()
    targets = data.setdefault("sales_targets", {})
    if amount is None:
        targets.pop(key, None)
    else:
        targets[key] = amount
    return targets


def get_sales_target(data: dict, member: str):
    return sales_targets(data).get(member)


def _phase_probability(data: dict, phase_name: str) -> float:
    pdef = _find_phase_def(opportunity_phases(data), phase_name)
    p = pdef.get("probability") if pdef else None
    return float(p) if isinstance(p, (int, float)) else 0.0


def weighted_pipeline(data: dict, *, assignee: Optional[str] = None) -> float:
    """見込み売上 = Σ (goal_amount × phase.probability / 100) over OPEN
    (non-terminal) opportunities. ``assignee`` scopes it to one member's
    pipeline. Deals with no amount or no phase probability contribute 0."""
    phases = opportunity_phases(data)
    term = {p.get("name") for p in phases if p.get("terminal")}
    total = 0.0
    for o in data.get("opportunities", []):
        if o.get("phase") in term:
            continue
        if assignee is not None and (o.get("assignee") or "") != assignee:
            continue
        amt = o.get("goal_amount")
        if isinstance(amt, (int, float)):
            total += amt * _phase_probability(data, o.get("phase")) / 100.0
    return total


# ---------------------------------------------------------------------------
# Transition date (遷移日) — ms-107 e-3371, SPEC §2. Target-class primitive.
# ---------------------------------------------------------------------------
# 遷移日 = 「そのフェーズのゴール達成を判定する予定日」. It sits alongside ``ball``
# on a target's runtime state and is the pivot of the engine cycle
# (置く → 準備 → 判定 → advance/retry/terminal). Written like ``phase_history``:
# the current value is mirrored on the target, and every change is appended to
# an append-only ``transition_date_history`` (証跡, data-immutability-principle).
# Generic over targets that close (opp- today); accounts (対象・継続) never do.

def get_transition_date(data: dict, target_id: str) -> str:
    """Current transition_date of a target (opp-…), or '' when unset."""
    if target_id.startswith("opp-"):
        opp = find_opportunity(data, target_id)
        if opp is None:
            raise ValueError(f"Opportunity not found: {target_id}")
        return opp.get("transition_date", "") or ""
    raise ValueError(
        f"transition_date targets an opportunity (opp-…), got {target_id!r}")


def set_transition_date(data: dict, target_id: str, transition_date: str, *,
                        note: str = "", at: str = "") -> dict:
    """Set (or clear) a target's transition_date and log it append-only.

    Passing an empty ``transition_date`` clears the field (still logged, so the
    clearing is auditable). Returns the appended history record. Permissive by
    design — the engine (e-3372) decides *when* a date is placed; this is only
    the storage primitive.
    """
    if target_id.startswith("opp-"):
        opp = find_opportunity(data, target_id)
        if opp is None:
            raise ValueError(f"Opportunity not found: {target_id}")
        value = (transition_date or "").strip()
        record = {"transition_date": value, "at": at, "note": note}
        opp.setdefault("transition_date_history", []).append(record)
        opp["transition_date"] = value
        return record
    raise ValueError(
        f"transition_date targets an opportunity (opp-…), got {target_id!r}")


def needs_transition_date(data: dict, target_id: str) -> bool:
    """True when a target sits in a non-terminal phase but has no transition_date
    (SPEC §2: placing the 遷移日 is the top-priority activity on phase entry —
    without a judgement date, preparation and follow-up can't be driven).

    Terminal-phase targets (決着済み) never need one, so this is False for them.
    """
    if not target_id.startswith("opp-"):
        return False
    opp = find_opportunity(data, target_id)
    if opp is None:
        return False
    if opportunity_phase_is_terminal(data, opp.get("phase", "")):
        return False
    return not (opp.get("transition_date") or "").strip()


# ---------------------------------------------------------------------------
# Transition judgement engine — ms-107 e-3372, SPEC §3. Target-class generic.
# ---------------------------------------------------------------------------
# 遷移日に達したら、そのフェーズのゴールを人間が判定する: advance (次へ) /
# retry (やり直し = 新しい遷移日) / terminal (決着). AI は候補を出すだけで状態は
# 変えない (master=人間, SPEC §6) — この層は「判定を適用する」プリミティブで、
# *いつ* 判定するか / *どの* 分岐かは呼び出し側 (Skill + 人間) が決める。
#
# transition_status は派生 (derived) — transition_date と「今日」から毎回導出し、
# 永続フラグを持たない。判定されないまま遷移日を過ぎた商談は自動で ``overdue``
# に分類され、非terminal な間はそこに居座る (= 人間が判定するまで催促し続ける)。
# 永続フラグにすると、日付が過ぎた瞬間にフラグを立て直す常駐処理が要り必ず
# stale になるが、派生なら常に正しい。``status`` (open/won/lost…) とは直交する
# 別次元 (overdue な商談も status は open のまま)。

TRANSITION_UNSET = "unset"          # 非terminal・遷移日なし (= needs_transition_date)
TRANSITION_SCHEDULED = "scheduled"  # 遷移日が未来
TRANSITION_DUE = "due"              # 遷移日が今日 (= 判定日)
TRANSITION_OVERDUE = "overdue"      # 遷移日を過ぎ、まだ非terminal (= 判定待ち, 抜け漏れ候補)
TRANSITION_SETTLED = "settled"      # terminal フェーズ (= 決着済み)


# --- target-class temporal core (ms-107 e-3271) ----------------------------
# 「対象が期日を持つ → 時間的ステータスが派生する」は sales 固有でなく target-class
# 共通の関心事 (milestone.target_date / opportunity.transition_date / operation の
# 次回発火 は同型)。開発 Beacon が締切を surface していないのは設計でなく gap。
# ここは *pure* な汎用コア (data も target 型も知らない、期日と『今日』と settled
# 述語だけ) として書き、営業は下の transition_status がこれを wrap する第一
# consumer。ms-109 (統合リファクタ) で L2 engine module へそのまま抽出し、
# milestone.target_date を second consumer として差す。

def temporal_status(due_date: str, today: str, *, settled: bool = False) -> str:
    """Pure temporal classification of a due date relative to ``today``
    (both ``YYYY-MM-DD``). Target-class generic — no entity knowledge.

    ``settled=True`` (= 決着済み / 完了) → SETTLED regardless of date. No due
    date → UNSET. Otherwise past → OVERDUE, today → DUE, future → SCHEDULED.
    ISO dates compare correctly as plain strings, so no parsing is needed.
    """
    if settled:
        return TRANSITION_SETTLED
    d = (due_date or "").strip()
    if not d:
        return TRANSITION_UNSET
    if d < today:
        return TRANSITION_OVERDUE
    if d == today:
        return TRANSITION_DUE
    return TRANSITION_SCHEDULED


def scan_overdue(items, due_date_of, settled_of, today: str) -> list:
    """Generic 締切精査 (deadline review): return ``(item, status)`` pairs whose
    temporal_status is DUE or OVERDUE, oldest due date first. ``due_date_of`` /
    ``settled_of`` are callables reading a due date / settled flag off each item,
    so any target class plugs in (opportunities today; milestones at ms-109)."""
    out = []
    for it in items:
        st = temporal_status(due_date_of(it), today, settled=bool(settled_of(it)))
        if st in (TRANSITION_DUE, TRANSITION_OVERDUE):
            out.append((it, st))
    out.sort(key=lambda pair: (due_date_of(pair[0]) or ""))
    return out


def transition_status(data: dict, target_id: str, today: str) -> str:
    """Derived judgement state of an opportunity relative to ``today`` — the
    sales wrapper over ``temporal_status`` (due date = transition_date, settled =
    terminal phase). Returns one of the TRANSITION_* constants."""
    opp = find_opportunity(data, target_id)
    if opp is None:
        raise ValueError(f"Opportunity not found: {target_id}")
    return temporal_status(
        opp.get("transition_date", ""), today,
        settled=opportunity_phase_is_terminal(data, opp.get("phase", "")))


def next_opportunity_phase(data: dict, phase_name: str) -> Optional[str]:
    """The next *non-terminal* phase after ``phase_name`` in the configured
    funnel order, or None when ``phase_name`` is the last non-terminal stage,
    is unknown, or is itself terminal. Advance moves along this order; the
    success outcome after the final stage is a terminal (決着), which goes
    through the terminal path, not advance."""
    phases = opportunity_phases(data)
    names = _phase_names(phases)
    if phase_name not in names:
        return None
    idx = names.index(phase_name)
    for p in phases[idx + 1:]:
        if not p.get("terminal"):
            return p.get("name")
    return None


def advance_transition(data: dict, target_id: str, *,
                       next_transition_date: str = "", note: str = "",
                       at: str = "") -> dict:
    """Advance a target to the next non-terminal phase (goal met, SPEC §3).

    Consumes the current transition_date: sets ``next_transition_date`` on the
    new phase when provided, else clears it so ``needs_transition_date`` prompts
    for a fresh one (SPEC §2). Returns ``{"phase": <new>, "transition_date": …}``.
    Raises ValueError when there is no next non-terminal phase (the caller should
    use the terminal path — 成約 等 — instead of advance).
    """
    opp = find_opportunity(data, target_id)
    if opp is None:
        raise ValueError(f"Opportunity not found: {target_id}")
    cur = opp.get("phase", "")
    nxt = next_opportunity_phase(data, cur)
    if nxt is None:
        raise ValueError(
            f"'{cur}' は最終ステージです (次の非terminalフェーズがありません)。"
            "advance ではなく terminal (決着) を宣言してください")
    phase_set(data, target_id, nxt, note=note, at=at)
    set_transition_date(data, target_id, next_transition_date,
                        note=note or "advance", at=at)
    # e-3270: フェーズ入場でそのフェーズの固定アンカー活動を起こす (提案準備→提案
    # 作成 等)。goal からの文脈生成は Skill 層が上乗せする (LLM 必要)。共通アンカー
    # 「遷移日を置く」は needs_transition_date 促しで担い、ここでは重複させない。
    created = instantiate_phase_activities(data, target_id, at=at)
    return {"phase": nxt, "transition_date": opp.get("transition_date", ""),
            "activities": created}


def retry_transition(data: dict, target_id: str, new_transition_date: str, *,
                     note: str = "", at: str = "") -> dict:
    """Stay in the same phase but place a new transition_date (未達だが粘る,
    SPEC §3). ``new_transition_date`` is required — retry means 置き直す, so an
    empty date is rejected (that would be a clear, not a retry). Returns the
    appended transition_date_history record."""
    if not (new_transition_date or "").strip():
        raise ValueError("retry requires a new transition_date (置き直す日付)")
    return set_transition_date(data, target_id, new_transition_date,
                               note=note or "retry", at=at)


def allowed_terminals_for(data: dict, target_id: str) -> list:
    """The terminal (決着) phases declarable from the target's current phase
    (its ``allowed_terminals`` rule), or [] when unknown/unset. This is the
    choice list a human picks from on a failed judgement (SPEC §3)."""
    opp = find_opportunity(data, target_id)
    if opp is None:
        raise ValueError(f"Opportunity not found: {target_id}")
    cur_def = _find_phase_def(opportunity_phases(data), opp.get("phase", ""))
    return list((cur_def or {}).get("allowed_terminals") or [])


def terminal_transition(data: dict, target_id: str, terminal_phase: str, *,
                        note: str = "", at: str = "") -> dict:
    """Declare a terminal (決着) phase and consume the transition_date.

    Permissive (master=人間, SPEC §6): declaring a terminal outside the stage's
    ``allowed_terminals`` is surfaced by ``opportunity_phase_warnings`` (caller's
    job), never blocked here. This helper only enforces that the target phase is
    actually terminal (a typo'd stage name would silently leave the deal open).
    Returns the phase_history record.
    """
    if not opportunity_phase_is_terminal(data, terminal_phase):
        raise ValueError(
            f"'{terminal_phase}' は terminal (決着) フェーズではありません "
            f"(既知の決着: {[p['name'] for p in opportunity_phases(data) if p.get('terminal')]})")
    rec = phase_set(data, target_id, terminal_phase, note=note or "terminal", at=at)
    # 決着したら遷移日は用済み — 証跡を残して消す (settled は date を持たない)。
    set_transition_date(data, target_id, "", note="settled", at=at)
    return rec


def suggest_transition_date(data: dict, phase_name: str,
                            base_date: str) -> Optional[str]:
    """Suggest a transition_date for a phase = ``base_date`` + the phase's
    ``default_lead`` days, or None when the phase has no default_lead. Pure
    date arithmetic; the human still confirms (this is a suggestion, SPEC §6).
    ``base_date`` is ``YYYY-MM-DD``."""
    pdef = _find_phase_def(opportunity_phases(data), phase_name)
    lead = phase_default_lead(pdef)
    if lead is None:
        return None
    import datetime
    try:
        base = datetime.date.fromisoformat(base_date)
    except (TypeError, ValueError):
        return None
    return (base + datetime.timedelta(days=lead)).isoformat()


def opportunities_awaiting_judgement(data: dict, today: str) -> list:
    """締切精査 for opportunities: due/overdue deals as of ``today``, oldest
    first. Built on the generic ``scan_overdue`` (transition_date + terminal =
    settled). Each row carries ``who_has_the_ball`` so the sales view can split
    the overdue set into the two actions it implies (SPEC §3 + e-3271):

    * ball=self  → 自分が判定/対応する (判定待ち)
    * ball=counterpart → 相手待ちが期限超過 → 催促する (相手ボール timeout)

    Returns dicts with id / title / phase / transition_date / transition_status /
    who_has_the_ball. This is the AI's catch surface for the ``overdue`` state."""
    def due_of(o):
        return o.get("transition_date", "")

    def settled_of(o):
        return opportunity_phase_is_terminal(data, o.get("phase", ""))

    pairs = scan_overdue(data.get("opportunities", []), due_of, settled_of, today)
    return [{
        "id": o["id"],
        "title": o.get("title", ""),
        "phase": o.get("phase", ""),
        "transition_date": o.get("transition_date", ""),
        "transition_status": st,
        "who_has_the_ball": o.get("who_has_the_ball", ""),
    } for o, st in pairs]


# ---------------------------------------------------------------------------
# Activity (業務・事前計画型) — 従属 composition → Opportunity
# ---------------------------------------------------------------------------

def activity_add(data: dict, opportunity_id: str, description: str, *,
                 deadline: str = "", who_has_the_ball: str = BALL_SELF,
                 source: str = "", created_at: str = "") -> str:
    """Append an Activity (業務・事前計画型) under an Opportunity, return its id.

    ``source`` records where the activity came from (e.g. ``"template-anchor"``
    for a phase's fixed step, ``"ai"`` for an AI-generated one, "" for a hand
    -added one) so the origin stays auditable."""
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
        "source": source,
        "created_at": created_at,
    })
    return act_id


def next_nurturing_id(data: dict) -> str:
    ids = []
    for acc in data.get("accounts", []):
        ids.extend(n.get("id", "") for n in acc.get("nurturings", []))
    return _next_prefixed_id(ids, "nrt-")


def nurturing_add(data: dict, account_id: str, description: str, *,
                  deadline: str = "", who_has_the_ball: str = BALL_SELF,
                  source: str = "", created_at: str = "") -> str:
    """Append a Nurturing (継続関係の業務・事前計画型) under an Account, return
    its id. The continuous-target twin of ``activity_add``: an Opportunity
    carries 業務 as *activities*, an Account carries them as *nurturings*
    (ms-106 fb3). Same shape (description / deadline / status / ball / source)
    so the target-class engine (ms-109) can treat both uniformly; the naming
    just reflects that account work is 関係維持 (nurturing), not deal advance.
    """
    acc = find_account(data, account_id)
    if acc is None:
        raise ValueError(f"Account not found: {account_id}")
    if not description or not description.strip():
        raise ValueError("Nurturing description is required")
    if who_has_the_ball not in VALID_BALL:
        raise ValueError(
            f"who_has_the_ball must be one of {sorted(VALID_BALL)}, got {who_has_the_ball!r}")
    nrt_id = next_nurturing_id(data)
    acc.setdefault("nurturings", []).append({
        "id": nrt_id,
        "description": description.strip(),
        "deadline": deadline,
        "status": "todo",
        "who_has_the_ball": who_has_the_ball,
        "source": source,
        "created_at": created_at,
    })
    return nrt_id


def instantiate_phase_activities(data: dict, target_id: str, *,
                                 at: str = "") -> list:
    """Create the current phase's fixed anchor activities on a target (e-3270,
    SPEC §4). Anchors come from the phase's ``activity_template`` — the company's
    must-do steps for that phase (提案準備→提案作成, 合意済み→契約書送付 等). Returns
    the created activity ids.

    Idempotent by description: an anchor whose text already exists as a
    non-done activity on this target is skipped, so re-entering a phase (or
    calling twice) never duplicates. This layer is deterministic — the AI's
    goal-driven activities (contextual, per-deal) are added on top by the Skill.
    """
    opp = find_opportunity(data, target_id)
    if opp is None:
        raise ValueError(f"Opportunity not found: {target_id}")
    pdef = _find_phase_def(opportunity_phases(data), opp.get("phase", ""))
    anchors = phase_activity_template(pdef)
    existing = {(_norm(a.get("description")))
                for a in opp.get("activities", []) if a.get("status") != "done"}
    created = []
    for desc in anchors:
        text = str(desc).strip()
        if not text or _norm(text) in existing:
            continue
        created.append(activity_add(data, target_id, text,
                                    source="template-anchor", created_at=at))
        existing.add(_norm(text))
    return created


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
    """Remove an Opportunity and its composition children (activities and
    communications go with it — they have no life independent of the deal)."""
    if find_opportunity(data, opportunity_id) is None:
        raise ValueError(f"Opportunity not found: {opportunity_id}")
    data["opportunities"] = [o for o in data.get("opportunities", [])
                             if o.get("id") != opportunity_id]


# ---------------------------------------------------------------------------
# Communication (証跡・事後記録型) — 従属 composition → Opportunity or Account
# ---------------------------------------------------------------------------
# ms-107 e-3432 / SPEC o83GEljD8xeFMr95wLTh 設計方針 1: 営業の Commit。
#
# 開発の Task→Commit と同型で、営業の Activity (事前計画型) と対をなす事後記録型。
# 「実際にこのメールを送り、こう返ってきた」を source を辿れる形 + AI 要約付きで
# 残す証跡。フェーズ遷移の判定は「予定 (Activity)」でなく「事実 (Communication)」
# を読むのが正しい (SPEC §1)。性質:
#   - source を辿れる (message-id / thread / permalink / 議事録 doc / event link)
#   - AI 要約付き (summary は 1 行)
#   - 不変・追記のみ (data-immutability-principle — 一度残した証跡は書き換えない)
#   - target (Opportunity 優先、商談が無ければ Account) の子。Activity と同じ子層。
#
# ms-109 含意: Commit ≡ Communication = target 横断で汎用な証跡 primitive。本 MS
# では営業アダプタとして実装し、汎用化は ms-109 に委ねる。

COMM_INBOUND = "inbound"        # 相手 → 自分 (受信): この後ボールは自分
COMM_OUTBOUND = "outbound"      # 自分 → 相手 (送信): この後ボールは相手
VALID_COMM_DIRECTION = {COMM_INBOUND, COMM_OUTBOUND}

# やり取りの媒体。e-3454: channel は自由記述 (現実の媒体は email/slack に収まらず
# Facebook Messenger / LINE / 対面 / 電話 等と開いている)。下は UI hint / 補完候補
# としての *既知* セットであって enforce される閉集合ではない。communication_add は
# 任意の非空文字列を受け付け、正規化 (strip + lowercase) のみ行う (空なら "other")。
KNOWN_COMM_CHANNELS = ("email", "slack", "meeting", "calendar", "phone",
                       "messenger", "line", "in-person", "sms", "other")
# 後方互換 alias (旧名参照コード用)。
COMM_CHANNELS = KNOWN_COMM_CHANNELS


def _gather_communications(target: dict) -> list:
    """All communications of a target (opp/acc): its own target-level records
    plus those nested under its work items (activities/nurturings). Insertion
    order across the two levels is preserved for the stable-sort fallback in
    ``communications_of``. e-3503."""
    out = list((target or {}).get("communications", []) or [])
    for child in (target or {}).get("activities", []) or []:
        out.extend(child.get("communications", []) or [])
    for child in (target or {}).get("nurturings", []) or []:
        out.extend(child.get("communications", []) or [])
    return out


def next_communication_id(data: dict) -> str:
    ids = []
    for opp in data.get("opportunities", []):
        ids.extend(c.get("id", "") for c in _gather_communications(opp))
    for acc in data.get("accounts", []):
        ids.extend(c.get("id", "") for c in _gather_communications(acc))
    return _next_prefixed_id(ids, "comm-")


def find_activity(data: dict, activity_id: str):
    """Return ``(opportunity, activity)`` for an activity id, or ``(None, None)``."""
    for opp in data.get("opportunities", []):
        for a in opp.get("activities", []):
            if a.get("id") == activity_id:
                return opp, a
    return None, None


VALID_ACTIVITY_STATUS = {"todo", "done"}


def activity_set_status(data: dict, activity_id: str, status: str, *,
                        at: str = "") -> dict:
    """Set an Activity's status (todo/done) and return it. Used when a send or
    other Communication *fulfills* a planned Activity (ms-106 e-3505): the plan
    is marked done rather than leaving a lingering todo sitting beside the
    Communication fact — the send is recorded once (as the Communication), and
    the plan it satisfied is closed, not duplicated as a second '[sent]' todo."""
    opp, act = find_activity(data, activity_id)
    if act is None:
        raise ValueError(f"Activity not found: {activity_id}")
    if status not in VALID_ACTIVITY_STATUS:
        raise ValueError(
            f"status must be one of {sorted(VALID_ACTIVITY_STATUS)}, got {status!r}")
    act["status"] = status
    if status == "done":
        act["done_at"] = at
    return act


def activity_cancel(data: dict, activity_id: str, *, reason: str = "") -> dict:
    """Cancel (取消) an Activity and return it — correcting a mis-recorded plan
    without deleting it (data-immutability-principle).

    Routes through the shared ``work_base.stamp_cancel`` (ms-109 e-3558): sets
    ``status="cancelled"`` and stamps ``meta.cancelled_at / cancelled_by /
    cancel_reason``, append-only. This is the sales side of the same cancel
    vocabulary development uses (``core.task_delete``), closing the historical
    gap where sales could only hard-delete (SPEC AC3).

    Scope note (e-3558): this lands the cancel primitive for the Activity
    work item. Turning the Account/Opportunity hard-deletes into soft-cancel,
    the Communication delete/retarget path, and the reader-side filtering of
    cancelled records belong to e-3537 (誤起票の訂正を primitive 化).
    """
    _, act = find_activity(data, activity_id)
    if act is None:
        raise ValueError(f"Activity not found: {activity_id}")
    return work_base.stamp_cancel(act, reason=reason)


def find_nurturing(data: dict, nurturing_id: str):
    """Return ``(account, nurturing)`` for a nurturing id, or ``(None, None)``."""
    for acc in data.get("accounts", []):
        for n in acc.get("nurturings", []):
            if n.get("id") == nurturing_id:
                return acc, n
    return None, None


def find_work_item(data: dict, work_item_id: str):
    """Resolve an Activity (act-) or Nurturing (nrt-) work item to
    ``(target, work_item)``, or ``(None, None)``. Used by the reply-watcher
    (E) which watches a specific 予定 for its counterpart's reply."""
    if work_item_id.startswith("act-"):
        return find_activity(data, work_item_id)
    if work_item_id.startswith("nrt-"):
        return find_nurturing(data, work_item_id)
    return None, None


# ---------------------------------------------------------------------------
# Watch (返信待ちスレッドの見張りフラグ) — E (返信ウォッチャー) の状態
# ---------------------------------------------------------------------------
# ms-107 e-3437。時間に敏感なやり取り (日程打診など) を送った時、その予定
# (Activity / Nurturing) に watch を立てる。返信ウォッチャー (E) が hourly に
# 「watch あり かつ ball=相手 (= まだ返信待ち)」のスレッドだけ確認し、返信が
# 来たら inbound Communication を残して ball を自分に戻す (= derive_ball が flip)。
# 話題が完結したら watch を落とす。watch 自体は「どのスレッドを・どの媒体で
# 見るか」を持つだけで、定期起動の判定は tick_scheduler が担う (関心の分離)。

def set_watch(data: dict, work_item_id: str, *, channel: str,
              thread_ref: str = "", cadence_minutes: int = 60,
              at: str = "") -> dict:
    """Arm a reply-watch on a work item (act-/nrt-). ``channel`` is the medium
    to poll (email/slack…), ``thread_ref`` the thread/message id to look under
    (from the outbound Communication's source). Returns the watch dict."""
    target, wi = find_work_item(data, work_item_id)
    if wi is None:
        raise ValueError(f"Work item not found (act-/nrt-): {work_item_id}")
    if not channel or not channel.strip():
        raise ValueError("watch channel is required")
    wi["watch"] = {
        "enabled": True,
        "channel": channel.strip().lower(),
        "thread_ref": thread_ref,
        "cadence_minutes": int(cadence_minutes) if cadence_minutes else 60,
        "armed_at": at,
        "last_checked_at": "",
    }
    return wi["watch"]


def clear_watch(data: dict, work_item_id: str, *, at: str = "") -> None:
    """Disarm a reply-watch (話題が完結した時). Kept as ``enabled: False`` so
    the audit trail of "we were watching" survives (data-immutability-principle)."""
    target, wi = find_work_item(data, work_item_id)
    if wi is None:
        raise ValueError(f"Work item not found (act-/nrt-): {work_item_id}")
    w = wi.get("watch")
    if w:
        w["enabled"] = False
        w["cleared_at"] = at


def watched_work_items(data: dict, *, awaiting_reply_only: bool = False) -> list:
    """List armed reply-watches as ``(target, work_item)``. With
    ``awaiting_reply_only`` keep only those whose target's ball is the
    counterpart (= we're still waiting) — that's the set E actually polls
    ('ball=相手 かつ watch', SPEC §3). Others (ball already back to us) are
    skipped until we send again."""
    out = []
    for opp in data.get("opportunities", []):
        for a in opp.get("activities", []):
            w = a.get("watch")
            if w and w.get("enabled"):
                if awaiting_reply_only and derive_ball(opp) != BALL_COUNTERPART:
                    continue
                out.append((opp, a))
    for acc in data.get("accounts", []):
        for n in acc.get("nurturings", []):
            w = n.get("watch")
            if w and w.get("enabled"):
                if awaiting_reply_only and derive_ball(acc) != BALL_COUNTERPART:
                    continue
                out.append((acc, n))
    return out


def resolve_communication_target(data: dict, target_id: str):
    """Resolve where a Communication is stored and what planned item it fulfills.

    Mirrors the dev model where a commit is *stored under* a milestone yet can
    *reference* a task (resolves). Here the "container" (where the record lives)
    is always an Opportunity or Account; the optional "linked_id" points at the
    specific Activity/Nurturing (= 予定) the communication fulfilled:

      opp-…  → (opportunity, "")          # target grain, no work-item link
      acc-…  → (account,     "")          # target grain, no work-item link
      act-…  → (parent opp,  "act-…")     # stored on the deal, links the activity
      nrt-…  → (parent acc,  "nrt-…")     # stored on the account, links the nurturing

    Returns ``(container, linked_id)`` or ``(None, None)`` when unresolvable."""
    if target_id.startswith("opp-"):
        opp = find_opportunity(data, target_id)
        return (opp, "") if opp is not None else (None, None)
    if target_id.startswith("acc-"):
        acc = find_account(data, target_id)
        return (acc, "") if acc is not None else (None, None)
    if target_id.startswith("act-"):
        opp, act = find_activity(data, target_id)
        return (opp, target_id) if act is not None else (None, None)
    if target_id.startswith("nrt-"):
        acc, nrt = find_nurturing(data, target_id)
        return (acc, target_id) if nrt is not None else (None, None)
    return None, None


def find_communication_target(data: dict, target_id: str) -> Optional[dict]:
    """The container (Opportunity/Account) that stores a Communication for the
    given id — accepts opp-/acc- (target grain) and act-/nrt- (work-item grain,
    resolved to the parent deal/account). Returns the container dict or None."""
    container, _ = resolve_communication_target(data, target_id)
    return container


def communication_add(data: dict, target_id: str, summary: str, *,
                      direction: str, channel: str = "other",
                      source: Optional[dict] = None,
                      occurred_at: str = "", created_at: str = "") -> str:
    """Append a Communication (証跡・事後記録型) and return its id.

    ``target_id`` may be a target (opp-/acc-) or a planned work item
    (act-/nrt-). This mirrors the dev commit↔task model *including its nesting*
    (ms-106 e-3503): a commit that resolves a task is stored **under that task**
    (nested), and one that resolves nothing sits at milestone level. So here a
    communication that fulfills an Activity/Nurturing is nested **under that
    work item's own ``communications``**; one addressed to the deal/account
    directly sits at the opp/acc level. ``linked_id`` still records which work
    item it fulfilled, so the two grains stay traceable.

    ``direction`` (inbound/outbound) is required — it's what ball derivation and
    the reply-watcher (E) read. ``source`` is a free dict of trace pointers
    (typically ``{"ref": <message-id/thread>, "url": <permalink>}``) so the
    origin stays auditable. Append-only: never mutates an existing record."""
    container, linked_id = resolve_communication_target(data, target_id)
    if container is None:
        raise ValueError(
            "Communication target not found (opp-…/acc-… target or "
            f"act-…/nrt-… work item): {target_id}")
    if not summary or not summary.strip():
        raise ValueError("Communication summary is required")
    if direction not in VALID_COMM_DIRECTION:
        raise ValueError(
            f"direction must be one of {sorted(VALID_COMM_DIRECTION)}, "
            f"got {direction!r}")
    # channel is free-text (e-3454): real-world channels are open-ended
    # (messenger / line / 対面 …). Normalize only; empty → "other".
    ch = (channel or "").strip().lower() or "other"
    comm_id = next_communication_id(data)
    # e-3503 — nest under the fulfilled work item (act-/nrt-), else store at the
    # target (opp/acc) level, mirroring commit-under-task vs commit-at-milestone.
    if linked_id.startswith("act-"):
        _, node = find_activity(data, linked_id)
    elif linked_id.startswith("nrt-"):
        _, node = find_nurturing(data, linked_id)
    else:
        node = container
    node.setdefault("communications", []).append({
        "id": comm_id,
        "direction": direction,
        "channel": ch,
        "summary": summary.strip(),
        "source": dict(source) if source else {},
        "linked_id": linked_id,
        "occurred_at": occurred_at,
        "created_at": created_at,
    })
    return comm_id


def communications_of(target: dict, *, linked_id: Optional[str] = None) -> list:
    """The target's communications in occurrence order (oldest → newest).
    Sort key is occurred_at, falling back to created_at then insertion order so
    records without a timestamp keep their append order (stable). Pass
    ``linked_id`` to keep only the communications that fulfilled that specific
    Activity/Nurturing (= work-item grain view).

    e-3503: gathers both the target-level records and those nested under the
    target's work items (activities/nurturings), so the chronological log is
    complete regardless of nesting. Old records stored flat at target level (pre
    -nesting) are still included — read stays backward-compatible."""
    comms = _gather_communications(target)
    if linked_id is not None:
        comms = [c for c in comms if c.get("linked_id") == linked_id]

    def key(pair):
        idx, c = pair
        t = c.get("occurred_at") or c.get("created_at") or ""
        # Untimed records (t == "") sort *after* all dated ones, then keep
        # insertion order — a freshly-added note without a timestamp belongs at
        # the tail of the chronological log, not the head.
        return (t == "", t, idx)

    ordered = sorted(enumerate(comms), key=key)
    return [c for _, c in ordered]


def derive_ball(target: dict) -> Optional[str]:
    """Whose court the deal is in, derived from the latest Communication
    (SPEC §6): the newest inbound means the counterpart just played → the ball
    is ours (BALL_SELF); the newest outbound means we played → theirs
    (BALL_COUNTERPART). Returns None when there's no communication to derive
    from (ball is unknown, not a default). ball was removed from the UI but the
    engine keeps it as the reply-watcher's (E) driver."""
    comms = communications_of(target)
    if not comms:
        return None
    latest = comms[-1]
    if latest.get("direction") == COMM_INBOUND:
        return BALL_SELF
    if latest.get("direction") == COMM_OUTBOUND:
        return BALL_COUNTERPART
    return None


# ---------------------------------------------------------------------------
# Meeting (面談・運用状態型) — 従属 composition → Opportunity
# ---------------------------------------------------------------------------
# ms-107 e-3433 (B) / e-3374 の ID ハンドシェイク。予定確定側 (B) が生産し、
# 終了検知側 (C = e-3434) が消費する共有基盤。
#
# 予定を確定しても Beacon の遷移日 (= フェーズ達成を判定する予定日) と Google
# カレンダーが二重管理でズレる問題を、両者を 1 つの Meeting レコードで束ねて
# 解消する。Communication (不変・事後の証跡) と違い、Meeting は運用状態型 —
# scheduled → ended / cancelled と状態が動く。状態変化は history に追記して
# 監査可能にする (data-immutability-principle は「証跡は消さない」の意)。
#
# 識別 ID (mtg-N) がカレンダー予定の説明文に埋め込む handshake token になる。
# C はこの token でカレンダー予定 → 商談を突き合わせ、二重起動を status で防ぐ。

MEETING_SCHEDULED = "scheduled"
MEETING_ENDED = "ended"
MEETING_CANCELLED = "cancelled"
VALID_MEETING_STATUS = {MEETING_SCHEDULED, MEETING_ENDED, MEETING_CANCELLED}

# handshake token 書式。B が埋め込み、C が parse する — 両者がこの 1 箇所を
# 参照することで書式 drift を構造的に防ぐ (single source of truth)。
_MEETING_TAG_PREFIX = "beacon-meeting-id:"


def meeting_calendar_tag(meeting_id: str) -> str:
    """The handshake token embedded in a calendar event's description so the
    end-detector (C) can map the event back to this meeting/opportunity.
    B writes it, C parses it — both call this one helper (書式の単一真値源)."""
    return f"{_MEETING_TAG_PREFIX} {meeting_id}"


def parse_meeting_tag(text: str) -> Optional[str]:
    """Extract a meeting id (mtg-…) from a calendar event's description that was
    stamped by :func:`meeting_calendar_tag`, or None when absent."""
    if not text:
        return None
    marker = text.find(_MEETING_TAG_PREFIX)
    if marker < 0:
        return None
    rest = text[marker + len(_MEETING_TAG_PREFIX):].strip()
    token = rest.split()[0] if rest.split() else ""
    return token if token.startswith("mtg-") else None


def next_meeting_id(data: dict) -> str:
    ids = []
    for opp in data.get("opportunities", []):
        ids.extend(m.get("id", "") for m in opp.get("meetings", []))
    return _next_prefixed_id(ids, "mtg-")


def find_meeting(data: dict, meeting_id: str):
    """Return ``(opportunity, meeting)`` for a meeting id, or ``(None, None)``."""
    for opp in data.get("opportunities", []):
        for m in opp.get("meetings", []):
            if m.get("id") == meeting_id:
                return opp, m
    return None, None


def opportunity_meetings(opp: dict) -> list:
    """A target's meetings in scheduled-time order (earliest → latest); untimed
    records sort last, then insertion order (same rule as communications_of)."""
    meetings = (opp or {}).get("meetings", [])
    ordered = sorted(
        enumerate(meetings),
        key=lambda pair: (not (pair[1].get("scheduled_at") or ""),
                          pair[1].get("scheduled_at") or "", pair[0]),
    )
    return [m for _, m in ordered]


def _parse_dt(value: str):
    """Parse an ISO8601 datetime (offset or trailing Z) to an aware UTC
    datetime, or None when empty / date-only / unparseable. Meeting times carry
    a timezone offset, so string comparison is wrong (same instant, different
    offset sorts differently) — the end-detector must compare real instants."""
    if not value or not str(value).strip():
        return None
    import datetime
    txt = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(txt)
    except ValueError:
        return None
    if dt.tzinfo is None:  # naive → assume UTC (best effort)
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def meeting_effective_end(meeting: dict):
    """When a meeting is considered over: its end_at, or (no end set) its
    scheduled_at treated as a point-in-time. Returns an aware UTC datetime or
    None when neither is a parseable timestamp."""
    return _parse_dt(meeting.get("end_at")) or _parse_dt(meeting.get("scheduled_at"))


def scan_ended_meetings(data: dict, now: str) -> list:
    """The end-detector's (C, e-3434) core: meetings whose scheduled end has
    passed but are still ``scheduled`` — candidates to confirm on the calendar
    and hand to the follow-up workflow (A). Pure and timezone-aware.

    Idempotent by status: a meeting already ``ended``/``cancelled`` drops out,
    so re-running the detector never fires the same meeting twice (SPEC AC:
    同じミーティングを二重起動しない). The engine still calls
    :func:`meeting_mark_ended` (also idempotent) to record the transition.

    Returns a list of ``(opportunity, meeting)`` for the ended candidates,
    oldest end first (so the earliest-finished meeting is handled first)."""
    now_dt = _parse_dt(now)
    if now_dt is None:
        return []
    hits = []
    for opp in data.get("opportunities", []):
        for m in opp.get("meetings", []):
            if m.get("status") != MEETING_SCHEDULED:
                continue
            end = meeting_effective_end(m)
            if end is not None and end <= now_dt:
                hits.append((opp, m, end))
    hits.sort(key=lambda t: t[2])
    return [(opp, m) for opp, m, _ in hits]


def meeting_schedule(data: dict, opportunity_id: str, scheduled_at: str, *,
                     end_at: str = "", location: str = "",
                     calendar_event_id: str = "", calendar_namespace: str = "",
                     calendar_account: str = "", set_transition: bool = False,
                     at: str = "") -> str:
    """Book a Meeting under an Opportunity and return its id (mtg-…).

    When ``set_transition`` is true the opportunity's 遷移日 (transition_date) is
    moved to the meeting date in the *same* call, so the calendar plan and the
    phase-judgement date never drift apart (AC: 遷移日とカレンダーが同時更新).
    The returned id is the handshake token B stamps into the calendar event."""
    opp = find_opportunity(data, opportunity_id)
    if opp is None:
        raise ValueError(f"Opportunity not found: {opportunity_id}")
    if not scheduled_at or not scheduled_at.strip():
        raise ValueError("Meeting scheduled_at is required")
    mtg_id = next_meeting_id(data)
    when = scheduled_at.strip()
    opp.setdefault("meetings", []).append({
        "id": mtg_id,
        "scheduled_at": when,
        "end_at": end_at,
        "location": location,
        "calendar_event_id": calendar_event_id,
        "calendar_namespace": calendar_namespace,
        "calendar_account": calendar_account,
        "status": MEETING_SCHEDULED,
        "created_at": at,
        "history": [{"at": at, "action": "scheduled", "scheduled_at": when}],
    })
    if set_transition:
        # meeting date drives the phase-judgement date (transition_signal =
        # calendar_ended). Store the date portion (YYYY-MM-DD) as the 遷移日.
        set_transition_date(data, opportunity_id, when[:10],
                            note=f"面談確定 ({mtg_id})", at=at)
    return mtg_id


def meeting_reschedule(data: dict, meeting_id: str, scheduled_at: str, *,
                       end_at: Optional[str] = None, location: Optional[str] = None,
                       calendar_event_id: Optional[str] = None,
                       set_transition: bool = False, at: str = "") -> dict:
    """Move an existing meeting to a new time (予定変更), keeping the same
    handshake id so the calendar event and Beacon stay linked. Logs the change
    to ``history`` and, when ``set_transition``, moves the 遷移日 too so both
    follow the reschedule (AC: 予定変更時も両者が追従)."""
    opp, m = find_meeting(data, meeting_id)
    if m is None:
        raise ValueError(f"Meeting not found: {meeting_id}")
    if not scheduled_at or not scheduled_at.strip():
        raise ValueError("Meeting scheduled_at is required")
    when = scheduled_at.strip()
    m["scheduled_at"] = when
    if end_at is not None:
        m["end_at"] = end_at
    if location is not None:
        m["location"] = location
    if calendar_event_id is not None:
        m["calendar_event_id"] = calendar_event_id
    m["status"] = MEETING_SCHEDULED  # rescheduling revives a cancelled slot
    m.setdefault("history", []).append(
        {"at": at, "action": "rescheduled", "scheduled_at": when})
    if set_transition:
        set_transition_date(data, opp["id"], when[:10],
                            note=f"面談再調整 ({meeting_id})", at=at)
    return m


def meeting_mark_ended(data: dict, meeting_id: str, *, at: str = "") -> dict:
    """Mark a meeting ended (C calls this after detecting the calendar event
    finished). Idempotent: a meeting already ended stays ended and no duplicate
    history row is appended — this is the double-trigger guard for C's Operation
    (e-3434: 同じミーティングを二重起動しない)."""
    opp, m = find_meeting(data, meeting_id)
    if m is None:
        raise ValueError(f"Meeting not found: {meeting_id}")
    if m.get("status") == MEETING_ENDED:
        return m  # already ended — no-op, keeps C idempotent
    m["status"] = MEETING_ENDED
    m.setdefault("history", []).append({"at": at, "action": "ended"})
    return m


def meeting_cancel(data: dict, meeting_id: str, *, at: str = "") -> dict:
    """Cancel a scheduled meeting (予定取消). Logged to history; the calendar
    event removal is the Skill's job (this is the Beacon-side state change)."""
    opp, m = find_meeting(data, meeting_id)
    if m is None:
        raise ValueError(f"Meeting not found: {meeting_id}")
    m["status"] = MEETING_CANCELLED
    m.setdefault("history", []).append({"at": at, "action": "cancelled"})
    return m


# ---------------------------------------------------------------------------
# Send identity pin (ms-107 e-3353 — 複数 Google アカウントの取り違え防止)
# ---------------------------------------------------------------------------
# 会社用 / 個人用など複数の Google アカウントがあり、取り違え送信は顧客との
# 関係を壊す一発事故。プロジェクト (将来は商談単位でも) に「この identity で
# 送る」を pin し、送信系 Skill は送信前に必ず from を pin と照合する。ここは
# データと照合ロジックのみ (照合を呼ぶのは各送信 Skill の責務)。

SEND_IDENTITY_KEY = "send_identity"    # the *default* send label (or, legacy, a bare email)
SEND_ACCOUNTS_KEY = "send_accounts"    # 送信アカウント台帳: 自分の Google アカウント群

# Services the ledger routes to concrete MCP tools. Kept small and explicit —
# each送信/操作 Skill maps one of these to its MCP tool family. slack は
# namespace=workspace 切替 (gmail と同型、account 引数なし)。
SEND_SERVICES = ("gmail", "calendar", "drive", "slack")


def _norm(value) -> str:
    """Case/space-insensitive key for label/email matching."""
    return (value or "").strip().lower()


def get_send_identity(data: dict) -> str:
    """Return the default send label (or legacy bare email), or ''."""
    return data.get(SEND_IDENTITY_KEY, "") or ""


def set_send_identity(data: dict, identity: str) -> str:
    """Pin the *default* send label for this project. Returns the stored value.

    This is now a pointer into the ledger (``send_accounts``): the value should
    be a ledger *label*. For backward compat a bare email is still accepted —
    ``check_send_from`` falls back to a plain string compare when no ledger
    entry resolves for the value.
    """
    if not identity or not identity.strip():
        raise ValueError("send identity is required (email address or account label)")
    data[SEND_IDENTITY_KEY] = identity.strip()
    return data[SEND_IDENTITY_KEY]


# ---------------------------------------------------------------------------
# Send-account ledger (ms-107 e-3365 — label → {email, per-service MCP route})
# ---------------------------------------------------------------------------
# 「どのアカウントで送るか」を bare email から台帳に格上げする。bare email では
# ① calendar/drive は同一 namespace 内で account=alias 切替、gmail は namespace
# 切替、と service ごとに切替機構が非対称なこと、② 同じ人物が複数 namespace で
# 到達可能なこと、を表せない。台帳は label → {email, routes{service:{namespace,
# alias}}} を持ち、送信/操作 Skill は namespace を必ず ``resolve_route`` 経由で
# 引く。台帳を通らない送信経路が存在しない = 取り違えが物理的に起きない (SPEC §2)。
# 注意: ここでの "account" は自分の送信アカウント。顧客 ``accounts`` (Account
# 顧客エンティティ) とは別物なので別キー ``send_accounts`` に格納する。


def list_send_accounts(data: dict) -> list:
    """Return the send-account ledger (list of entries), possibly empty."""
    return list(data.get(SEND_ACCOUNTS_KEY, []) or [])


def get_send_account(data: dict, label_or_email: str):
    """Return the ledger entry matching a label OR email (case-insensitive), or None."""
    key = _norm(label_or_email)
    if not key:
        return None
    for a in list_send_accounts(data):
        if _norm(a.get("label")) == key or _norm(a.get("email")) == key:
            return a
    return None


def _clean_routes(routes) -> dict:
    """Keep only known services with a non-empty namespace; normalise shape."""
    out = {}
    for svc, r in (routes or {}).items():
        if svc not in SEND_SERVICES or not isinstance(r, dict):
            continue
        ns = (r.get("namespace") or "").strip()
        if not ns:
            continue
        entry = {"namespace": ns}
        alias = (r.get("alias") or "").strip()
        if alias:
            entry["alias"] = alias
        out[svc] = entry
    return out


def add_send_account(data: dict, label: str, email: str, routes=None) -> dict:
    """Add (or idempotently update) a send account in the ledger.

    Matching an existing label updates email/routes in place so a Skill can
    re-run without creating duplicates. Returns the stored entry.
    """
    label = (label or "").strip()
    email = (email or "").strip()
    if not label:
        raise ValueError("account label is required")
    if not email:
        raise ValueError("account email is required")
    ledger = data.setdefault(SEND_ACCOUNTS_KEY, [])
    for a in ledger:
        if _norm(a.get("label")) == _norm(label):
            a["email"] = email
            if routes is not None:
                a["routes"] = _clean_routes(routes)
            return a
    entry = {"label": label, "email": email, "routes": _clean_routes(routes or {})}
    ledger.append(entry)
    return entry


def set_account_route(data: dict, label: str, service: str,
                      namespace: str, alias: str = "") -> dict:
    """Set one service's MCP route (namespace [+ alias]) on a ledger entry."""
    if service not in SEND_SERVICES:
        raise ValueError(f"unknown service '{service}' (expected one of {list(SEND_SERVICES)})")
    a = get_send_account(data, label)
    if a is None:
        raise ValueError(f"send account not found: {label}")
    ns = (namespace or "").strip()
    if not ns:
        raise ValueError("namespace is required")
    route = {"namespace": ns}
    alias = (alias or "").strip()
    if alias:
        route["alias"] = alias
    a.setdefault("routes", {})[service] = route
    return route


def remove_send_account(data: dict, label: str) -> None:
    """Remove a ledger entry by label (or email). Raises if not found."""
    key = _norm(label)
    ledger = list_send_accounts(data)
    kept = [a for a in ledger
            if _norm(a.get("label")) != key and _norm(a.get("email")) != key]
    if len(kept) == len(ledger):
        raise ValueError(f"send account not found: {label}")
    data[SEND_ACCOUNTS_KEY] = kept


def resolve_route(data: dict, service: str, label: str = "") -> Optional[dict]:
    """Resolve the concrete MCP routing for ``service`` for a given label
    (or the default send label when ``label`` is empty).

    Returns ``{label, email, service, namespace, alias}`` or ``None`` when the
    account or its route for that service is not configured. **This is the only
    sanctioned way a send/操作 Skill obtains a namespace** — a Skill must never
    free-hand one (that would reopen the取り違え hole, SPEC §2).
    """
    if service not in SEND_SERVICES:
        raise ValueError(f"unknown service '{service}' (expected one of {list(SEND_SERVICES)})")
    target = label.strip() if (label and label.strip()) else get_send_identity(data)
    a = get_send_account(data, target)
    if a is None:
        return None
    route = (a.get("routes") or {}).get(service)
    if not route or not route.get("namespace"):
        return None
    return {
        "label": a.get("label"),
        "email": a.get("email"),
        "service": service,
        "namespace": route.get("namespace"),
        "alias": route.get("alias"),
    }


def check_send_from(data: dict, from_value: str, label: str = "") -> tuple:
    """Compare a proposed send ``from`` against the resolved send identity.

    Returns ``(ok, message)``. The send Skill calls this immediately before
    sending and surfaces the message; on ``ok == False`` it must stop and let
    the human resolve (取り違え防止は仕組みで閉じる、SPEC §2)。

    Resolution order:
    * ``label`` (or the default send label) resolves to a *ledger entry* →
      compare ``from`` against that entry's **email** (the台帳 is authoritative).
    * No ledger entry resolves → legacy bare-string pin compare (back-compat).

    Branches (either path):
    * Nothing pinned/configured → ok=False, ask to pin/register first.
    * from empty                → ok=False, from must be explicit.
    * matches (case/space-insensitive) → ok=True.
    * mismatch                  → ok=False, name both so the human sees it.
    """
    target = label.strip() if (label and label.strip()) else get_send_identity(data)
    entry = get_send_account(data, target) if target else None
    if entry is not None:
        expected = entry.get("email", "")
        who = entry.get("label", "")
        if not from_value or not from_value.strip():
            return (False, f"from が空です。台帳 '{who}' の identity は '{expected}'。"
                           "from を明示してください")
        if _norm(from_value) == _norm(expected):
            return (True, f"from='{from_value}' は台帳 '{who}' ({expected}) と一致")
        return (False, f"from='{from_value}' が台帳 '{who}' ({expected}) と"
                       "一致しません。取り違えの恐れ。送信を止めます")
    # Legacy path: bare-string pin (no ledger entry for the pinned value).
    pinned = get_send_identity(data)
    if not pinned:
        return (False, "送信 identity が未設定です。先に pin してください "
                       "(取り違え防止のため、from を明示せず送信しません)")
    if not from_value or not from_value.strip():
        return (False, f"from が空です。pin された identity は '{pinned}'。"
                       "from を明示してください")
    if from_value.strip().lower() == pinned.strip().lower():
        return (True, f"from='{from_value}' は pin された identity と一致")
    return (False, f"from='{from_value}' が pin された identity '{pinned}' と"
                   "一致しません。取り違えの恐れ。送信を止めます")


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
