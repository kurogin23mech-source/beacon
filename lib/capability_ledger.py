"""Capability scope ledger — the L0..L4 sharing-scope classification of every
Beacon capability, plus the dependency invariant that a profession-shared
capability must not reach into one profession's concrete (ms-134 e-4719/e-4709).

WHY this exists
---------------
The application-map (CORE doc ``application-map``) records WHAT capabilities
exist, but carries no NORM for WHERE each belongs or WHAT it may depend on. So a
layer mix-up — a profession-shared feature reaching into a dev-specific concrete
(``doc`` depending on ``milestone``, bug e-4710) — was invisible until a human
hit it. This ledger turns the descriptive map into a NORMATIVE + machine-enforced
one: every capability gets a scope level, each level has a "what it may depend on"
invariant, and ``scripts/check-capability-scope.py`` flags violations.

The 5 sharing-scope levels (one axis: how widely a capability may be shared)
---------------------------------------------------------------------------
  L0  Beacon プロダクト運用 — admin / dev tooling for operating Beacon itself
      (not a general product feature; e.g. doctor / skill install / migrate).
  L1  全職種共通 — the coordination substrate, identical for every profession,
      does not concretize per profession (bus / dm / auth / trek / session).
  L2  クラス抽象化層 — operates on a Target via the ABSTRACTION; instantiated
      per profession (milestone in dev, opportunity in sales) but its RULE is
      profession-common (doc / claim / status / the target_* verbs).
  L3  職種固有デフォルト — a profession's built-in capabilities (dev: milestone /
      task / pr / deploy; sales: account / opportunity / meeting …).
  L4  プロジェクト個別最適 — capabilities built for one project only. None ship
      in the product by default; a project-local skill would be L4.

ORTHOGONAL axis — origin (ms-134 設計方針2): who authored a capability
(``beacon-default`` / ``client-custom`` / ``individual-via-trailnode``) is NOT a
scope level (the rejected L5). It rides as a separate column so promotion
(L4→L3) stays a scope move while authorship travels as metadata.

The dependency invariant (the one line the checker enforces)
------------------------------------------------------------
A capability may depend only on same-or-BROADER scope; it must NOT reach into a
narrower / profession-specific concrete. Concretely for the checker: an L1/L2
(profession-shared) capability must NOT call a profession-specific concrete
symbol (``core.save_entry`` / ``core.find_target_milestone`` — the dev milestone
recorder/resolver). Shared capabilities go through ``occupation.record_target_entry``
(the L2 abstraction) instead. That is exactly the boundary e-4720 closed for doc.

Terminology note: ``occupation.py``'s docstring uses "L1/L2/L3" in an OLDER,
different sense (occupation-coupling: L1=occupation-invariant, L2/L3=occupation-
specific from SPEC XOaDpSaFITVkZKKgPvPT). That older 3-way scheme lumped ``doc``
into "L1 occupation-invariant", which is why the milestone leak went unseen — it
had no "operates on the target abstraction" level. THIS module's L0..L4 is the
ms-134 sharing-scope scheme; its explicit L2 (class-abstraction) is the finer
distinction that catches the leak. The two schemes are distinct; do not conflate.

Pure data + pure transforms. The live-surface enumeration is delegated to
``cli_surface.enumerate_cli_verbs`` — the same single source of truth the verb
ledger and map-drift lint use — so this ledger reconciles against exactly that
surface (a test pins full coverage).
"""

from __future__ import annotations

import os
from typing import Optional

import cli_surface

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# The scope levels and the origin axis.
# ---------------------------------------------------------------------------

SCOPE_LEVELS = {
    "L0": "Beacon プロダクト運用 (admin/dev tooling — 一般機能ではない)",
    "L1": "全職種共通 (職種で具象化しない協奏基盤)",
    "L2": "クラス抽象化層 (target 抽象に触れ職種ごとに具象化されるが規則は職種共通)",
    "L3": "職種固有デフォルト",
    "L4": "プロジェクト個別最適",
}

ORIGINS = {"beacon-default", "client-custom", "individual-via-trailnode"}

# Sharing breadth for the dependency partial order (bigger = shared more widely),
# on the SINGLE sharing-scope axis. L0 is not given a breadth rank here because
# its rule is asymmetric rather than a different axis: an L0 (product-operation)
# capability may depend on any public level, but no public (L1..L4) capability
# may depend on L0, and L0 must not ship in the public distribution. ``may_depend``
# encodes that L0 rule explicitly (it is still the same sharing-scope axis, just a
# rank whose dependency edge only points inward). Reworded per philosophy review
# 2026-08-02 #5 so "L0 special-case" is not misread as "L0 is a second axis".
_SCOPE_BREADTH = {"L1": 4, "L2": 3, "L3": 2, "L4": 1}

# Profession-shared scopes: capabilities here must not reach a profession concrete.
PROFESSION_SHARED_SCOPES = ("L1", "L2")

# The profession-specific concrete symbols an L1/L2 capability must NOT call.
# Each maps to the abstraction it should use instead (surfaced in the violation
# message so a fix is one step away, per ms-115 方針5). Both profession sides are
# listed so the invariant is SYMMETRIC (philosophy review 2026-08-02 #1): a shared
# capability reaching a dev concrete (milestone recorder) OR a sales concrete
# (activity / communication / nurturing recorder) is flagged. Existence /
# validation and recording both route through the occupation layer
# (``occupation.is_valid_link_target`` / ``occupation.record_target_entry``) instead.
PROFESSION_CONCRETE_SYMBOLS = {
    # dev (milestone) concretes
    "core.save_entry":
        "dev milestone changelog recorder — use occupation.record_target_entry",
    "core.find_target_milestone":
        "dev milestone resolver — record via occupation.record_target_entry",
    # sales concretes (symmetric side)
    "sales_entities.activity_add":
        "sales activity recorder — a shared capability must record via "
        "occupation.record_target_entry, not a profession concrete",
    "sales_entities.communication_add":
        "sales communication recorder — record via occupation.record_target_entry",
    "sales_entities.nurturing_add":
        "sales nurturing recorder — record via occupation.record_target_entry",
}


# ---------------------------------------------------------------------------
# Non-enumerated profession coupling (ms-134 e-4740 / philosophy review #1).
#
# The symbol denylist above catches a shared capability CALLING a profession
# recorder. But the same leak also happens as a raw dict READ: indexing a
# profession-specific project-data collection directly (``data["milestones"]`` /
# ``data.get("opportunities")``) couples an L1/L2 capability to one profession's
# concrete storage, which the enumerated-symbol check cannot see. A shared
# capability that walks only ``milestones`` silently misses a sales project's
# targets (opportunities / accounts) — exactly the class of leak that hid the
# original doc→milestone bug. A profession-shared capability must enumerate
# targets through the occupation / work_model abstraction (the ``TARGET_ARMS``
# registry) instead of indexing a profession collection.
#
# NOTE: ``operations`` is deliberately NOT here — it is the L1 cross-profession
# scheduling collection, not a profession concrete, so an L1 handler reading it
# is legitimate.
# ---------------------------------------------------------------------------

# Each advice names the CONCRETE callable to use instead (like
# PROFESSION_CONCRETE_SYMBOLS names ``occupation.record_target_entry``), so a
# remediator has a one-step fix path, not a concept to go hunt for:
# ``occupation.iter_target_records(data)`` returns every Target record across
# professions, and ``occupation.target_collections(data)`` returns the collection
# keys — both walk dev + sales + descriptor-defined targets without branching.
PROFESSION_CONCRETE_COLLECTIONS = {
    "milestones":
        "dev milestone collection — enumerate targets via "
        "occupation.iter_target_records(data), not data['milestones'] directly",
    "opportunities":
        "sales opportunity collection — enumerate via "
        "occupation.iter_target_records(data), not data['opportunities'] directly",
    "accounts":
        "sales account collection — enumerate via "
        "occupation.iter_target_records(data), not data['accounts'] directly",
    "acquisitions":
        "sales acquisition collection — enumerate via "
        "occupation.iter_target_records(data), not data['acquisitions'] directly",
}

# Ratchet allowlist: (verb, collection) couplings that ALREADY exist and are
# accepted PENDING remediation (ms-134 e-4740). The checker reports these as
# visible "pending debt" but does NOT fail on them; any coupling NOT in this set
# is a NEW violation and fails CI. This is a ONE-WAY ratchet:
#   - remove an entry when its handler is migrated to the abstraction;
#   - NEVER add a new entry to silence a fresh violation — fix the handler.
# A test (``test_no_stale_collection_allowlist_entries``) fails if an entry here
# is no longer detected, so a remediation is forced to delete its allowlist row
# (the allowlist cannot rot into a lie about what still couples).
#
# Every entry below is a real coupling found in the 2026-08-02 sweep (the
# session-log ``target_list`` finding + the wider scan it seeded): a shared
# capability that walks only dev ``milestones`` and would miss a sales project's
# targets. Remediation is tracked under ms-134.
KNOWN_COLLECTION_COUPLING = {
    ("target_list", "milestones"),
    ("session_end", "milestones"),
    ("session_fork", "milestones"),
    ("session_rescue", "milestones"),
    ("cloud_migrate_from_local", "milestones"),
    ("trek_show", "milestones"),
    ("trek_timeline", "milestones"),
}


def is_known_collection_coupling(verb: str, collection: str) -> bool:
    """True when (verb, collection) is an accepted-pending coupling in the
    ratchet allowlist (reported as debt, not a CI failure)."""
    return (verb, collection) in KNOWN_COLLECTION_COUPLING


# ---------------------------------------------------------------------------
# Classification by verb noun (the substring before the first "_"). The rule map
# gives every current noun a scope; a per-verb OVERRIDE handles the exceptions
# where a noun's verbs split across scopes. A noun the rules do not know resolves
# to "" so ``reconcile`` surfaces it as unclassified — the same drift-catch the
# verb ledger uses for a newly-added verb.
# ---------------------------------------------------------------------------

# noun -> scope. Judgement recorded here is auditable and refinable; the checker
# only distinguishes profession-shared (L1/L2) from profession-specific (L0/L3/L4),
# so the dev/sales split inside L3 is documentation, not enforcement.
_NOUN_SCOPE = {
    # L0 — Beacon product operation / admin / dev tooling.
    "doctor": "L0", "skill": "L0", "migrate": "L0", "master": "L0",
    "reset": "L0", "update": "L0", "project": "L0",
    # L1 — all-profession coordination substrate (target-agnostic).
    "auth": "L1", "bus": "L1", "channel": "L1", "cloud": "L1", "cycle": "L1",
    "disclose": "L1", "undisclose": "L1", "dm": "L1", "help": "L1",
    "incident": "L1", "init": "L1", "member": "L1", "note": "L1",
    "onboarding": "L1", "operation": "L1", "org": "L1", "resume": "L1",
    "run": "L1", "search": "L1", "session": "L1", "sessions": "L1",
    "stop": "L1", "trek": "L1", "trigger": "L1", "watch": "L1",
    # L2 — class-abstraction: operate on a Target via the abstraction.
    "claim": "L2", "doc": "L2", "review": "L2", "status": "L2",
    "summary": "L2", "target": "L2",
    # L3 — profession default (dev).
    "milestone": "L3", "task": "L3", "log": "L3", "save": "L3", "sync": "L3",
    "push": "L3", "deploy": "L3", "pr": "L3", "issue": "L3", "retro": "L3",
    "rollback": "L3", "entry": "L3", "stuck": "L3",
    # L3 — profession default (sales).
    "account": "L3", "acquisition": "L3", "activity": "L3",
    "communication": "L3", "meeting": "L3", "nurturing": "L3",
    "opportunity": "L3", "phase": "L3", "sales": "L3", "contact": "L3",
    "dossier": "L3", "morning": "L3", "profile": "L3",
}

# Per-verb overrides where a single verb does not follow its noun's scope.
_VERB_SCOPE_OVERRIDE: dict = {
    # (none yet — the noun rules cover every current verb. Add here when a verb's
    # scope diverges from its noun, e.g. a future ``bus_*`` verb that is admin-only.)
}


def scope_of(cap_key: str) -> str:
    """Return the L0..L4 scope of a capability (CLI verb dispatch key), or ``""``
    when its noun is unknown to the rules (surfaced by ``reconcile`` as
    unclassified). A per-verb override wins over the noun rule."""
    key = (cap_key or "").strip()
    if key in _VERB_SCOPE_OVERRIDE:
        return _VERB_SCOPE_OVERRIDE[key]
    noun = key.split("_", 1)[0]
    return _NOUN_SCOPE.get(noun, "")


def origin_of(cap_key: str) -> str:
    """Return a capability's origin. Everything shipped in this repo is
    ``beacon-default``; ``client-custom`` / ``individual-via-trailnode`` are
    stamped when a capability enters via a client project or TrailNode (the
    orthogonal authorship axis, ms-134 設計方針2). Kept as a function so the
    origin source can move to per-capability data without changing callers."""
    return "beacon-default"


def is_profession_shared(scope: str) -> bool:
    """True when ``scope`` is a profession-SHARED level (L1/L2) — the levels the
    dependency invariant polices for reaches into a profession concrete."""
    return scope in PROFESSION_SHARED_SCOPES


def may_depend(from_scope: str, on_scope: str) -> bool:
    """True when a capability at ``from_scope`` may depend on one at ``on_scope``.

    Rule: depend only on same-or-broader sharing scope (L1 broadest → L4
    narrowest). L0 (product operation) may depend on any public level but must
    not be depended ON by a public capability — so ``may_depend(public, "L0")``
    is False while ``may_depend("L0", public)`` is True."""
    if from_scope == "L0":
        return on_scope in _SCOPE_BREADTH or on_scope == "L0"
    if on_scope == "L0":
        return False
    a, b = _SCOPE_BREADTH.get(from_scope), _SCOPE_BREADTH.get(on_scope)
    if a is None or b is None:
        return False
    # broader = larger breadth; may depend only on same-or-broader (>= own).
    return b >= a


# ---------------------------------------------------------------------------
# Live-surface reconciliation (coverage) — every live verb must classify.
# ---------------------------------------------------------------------------

def enumerate_live_verbs(commands_path: str = "") -> set:
    """Enumerate the live CLI dispatch surface via the shared
    ``cli_surface.enumerate_cli_verbs`` (the single source of truth also used by
    the verb ledger and map-drift lint), so this ledger reconciles against
    exactly that surface."""
    return cli_surface.enumerate_cli_verbs(commands_path)


def reconcile(live: Optional[set] = None) -> dict:
    """Reconcile the scope classification against the live CLI surface. Returns
    ``{"unclassified": [...], "by_scope": {L0: n, ...}}``:

    - ``unclassified``: live verbs whose noun the rules do not know — a capability
      was added under a new noun and needs a scope rule (the drift this ledger
      exists to catch; e-4709 AC: every capability classified, none unclassified).
    - ``by_scope``: count of live verbs per scope, for a coverage summary.
    """
    live = enumerate_live_verbs() if live is None else live
    unclassified = sorted(v for v in live if not scope_of(v))
    by_scope: dict = {k: 0 for k in SCOPE_LEVELS}
    for v in live:
        s = scope_of(v)
        if s:
            by_scope[s] = by_scope.get(s, 0) + 1
    return {"unclassified": unclassified, "by_scope": by_scope}


def summary() -> dict:
    """Return a coverage summary: total live verbs, per-scope counts, and the
    count still unclassified."""
    rec = reconcile()
    return {
        "total": sum(rec["by_scope"].values()) + len(rec["unclassified"]),
        "by_scope": rec["by_scope"],
        "unclassified": len(rec["unclassified"]),
    }


# ---------------------------------------------------------------------------
# Skill classification (the second capability surface, e-4709 AC "CLI + Skill").
#
# Skills are markdown, not code, so the DEPENDENCY invariant checker (which scans
# code for a reach into a profession concrete) does not apply to them — a skill's
# scope is classified for the ledger / distribution story, and the code-level
# invariant is enforced on the CLI verbs a skill calls. Rules mirror the verb
# noun rules: sales skills are L3, the coordination-substrate skills L1, the
# planning/knowledge skills that operate on the shared Target/project abstraction
# L2, dev delivery-workflow skills L3, and repo-maintenance skills L0.
# ---------------------------------------------------------------------------

# Prefix rules, longest-prefix-wins, applied after exact overrides.
_SKILL_PREFIX_SCOPE = (
    ("beacon-sales-", "L3"),      # sales profession skills
    ("beacon-operation-", "L1"),  # operations = cross-profession scheduling
    ("beacon-trek-", "L1"),       # trek coordination substrate
    ("beacon-session-", "L1"),    # session management
    ("beacon-dm-", "L1"),         # cross-user DM
    ("beacon-review", "L2"),      # review* operate on shared targets (ms-119 職種中立)
    ("_beacon-", "L1"),           # methodology companions (shared authoring aids)
)

# Exact skill → scope (where the prefix rule does not fit).
_SKILL_SCOPE = {
    "beacon-archaeology": "L2", "beacon-bus-armed": "L1", "beacon-cloud": "L1",
    "beacon-deploy": "L3", "beacon-dispatch": "L1", "beacon-drift-check": "L0",
    "beacon-incident-report": "L1", "beacon-init": "L1", "beacon-log": "L3",
    "beacon-map": "L2", "beacon-member": "L1", "beacon-note": "L1",
    "beacon-onboard": "L1", "beacon-pr-create": "L3", "beacon-push": "L3",
    "beacon-retro": "L2", "beacon-retrospect": "L2", "beacon-roadmap": "L2",
    "beacon-spec": "L2", "beacon-task": "L3", "beacon-vision": "L2",
}


def skill_scope_of(skill_name: str) -> str:
    """Return the L0..L4 scope of a Skill (by its name, e.g. ``beacon-task``),
    or ``""`` when unknown. Exact override wins, then the longest matching prefix
    rule."""
    name = (skill_name or "").strip()
    if name in _SKILL_SCOPE:
        return _SKILL_SCOPE[name]
    best, best_len = "", -1
    for prefix, scope in _SKILL_PREFIX_SCOPE:
        if name.startswith(prefix) and len(prefix) > best_len:
            best, best_len = scope, len(prefix)
    return best


def enumerate_skills(skills_dir: str = "") -> list:
    """Return the sorted skill names (``*.md`` basenames) shipped in the repo
    ``skills/`` directory — the live skill surface this ledger reconciles
    against."""
    d = skills_dir or os.path.join(_REPO, "skills")
    if not os.path.isdir(d):
        return []
    return sorted(f[:-3] for f in os.listdir(d) if f.endswith(".md"))


def reconcile_skills(skills_dir: str = "") -> dict:
    """Reconcile skill classification against the live skill surface. Returns
    ``{"unclassified": [...], "by_scope": {...}}`` — a skill with no scope rule
    is surfaced so it gets classified (mirrors ``reconcile`` for verbs)."""
    skills = enumerate_skills(skills_dir)
    unclassified = sorted(s for s in skills if not skill_scope_of(s))
    by_scope: dict = {k: 0 for k in SCOPE_LEVELS}
    for s in skills:
        sc = skill_scope_of(s)
        if sc:
            by_scope[sc] = by_scope.get(sc, 0) + 1
    return {"unclassified": unclassified, "by_scope": by_scope}
