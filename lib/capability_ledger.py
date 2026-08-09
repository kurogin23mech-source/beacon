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
# Each remaining entry is a GENUINE gap confirmed by the 2026-08-03 per-site
# review (e-4737): a shared capability that walks only dev ``milestones`` and
# would miss a sales project's targets. Where the gap is a whole deferred effort
# it is tracked by its own milestone; the inline note says which.
KNOWN_COLLECTION_COUPLING = {
    # trek aggregation is dev-bound (commits/tasks) but Trek is a profession-
    # generic feature — it should aggregate work-item/evidence across all target
    # arms. Deferred until cross-profession demand is clear: ms-137 (waiting).
    ("trek_show", "milestones"),
    ("trek_timeline", "milestones"),
    # session rescue finds other sessions by session_id in target entries; sales
    # stores those under activities/communications arms, not ``entries``. Needs
    # arm-aware walking. Deferred: ms-138 (waiting).
    ("session_rescue", "milestones"),
    # cloud migration pre-flight diffs only milestones; a sales-project migration
    # would not diff opportunities. A real gap, but the pre-flight ABORTS on any
    # local-only entry, so extending it is behaviour-sensitive (migration
    # internals + integration test needed) — left as tracked debt under ms-134.
    ("cloud_migrate_from_local", "milestones"),
}

# Reviewed-legitimate reads (ms-134 e-4737): (verb, collection) reads a
# human-confirmed per-site review found CORRECT — the sought data lives ONLY in
# that collection by the data model's design, so the read is not profession
# coupling. DISTINCT from KNOWN_COLLECTION_COUPLING (pending debt): these are NOT
# debt and must NOT be "remediated" (routing them through the target abstraction
# would be WRONG — e.g. it would walk sales targets that never hold this data, or
# drop operations). Each entry carries the evidence.
#
# CONFIRMATION GATE (AX review 2026-08-03): "reviewed" means a HUMAN confirmed it
# via the PR independent-review + human-merge checkpoint (CLAUDE.md merge gate) —
# not the AI self-declaring. An entry added here appears in the PR diff and is
# independently reviewed before merge; the AI proposes with evidence, the human
# confirms on merge (the ms-134 layer-assignment ownership pattern). A deeper
# code-level gate (checker validates the evidence cites a real artifact) is
# possible future hardening, deferred — shared with the debt ratchet's same
# prose-gate limitation.
#
# WHICH LIST does a new (verb, collection) go in? Two binary questions:
#   1. Is that collection the ONLY place this data can live, by the data model?
#      → yes → REVIEWED_LEGITIMATE (the read is exact, not coupling).
#   2. Would routing through occupation.iter_target_records be CORRECT if done?
#      → yes → KNOWN_COLLECTION_COUPLING (genuine debt to remediate).
#      → no (wrong abstraction — would drop operations / walk irrelevant targets)
#        → REVIEWED_LEGITIMATE.
REVIEWED_LEGITIMATE_COLLECTION_READS = {
    ("target_list", "milestones"):
        "target-transition-approval entries live only in milestones (dev) and "
        "operations (cross-profession): requires_spine_approval() is False for "
        "sales opportunities (existing judge path), so no sales target holds "
        "them. NOTE occupation.iter_target_records does NOT cover operations "
        "(TARGET_COLLECTIONS = milestones+opportunities), so target_list must "
        "keep reading milestones+operations directly — routing through the "
        "abstraction would silently drop operation-hosted approvals.",
    ("session_end", "milestones"):
        "occupation (claim) is stored only on ms['occupation']; "
        "milestone_release_occupation is milestone-specific and sales entities "
        "carry no occupation field. Reading milestones is exact, not coupling.",
    ("session_fork", "milestones"):
        "fork is a git-worktree operation (creates .worktrees/<ms-id>-fork-…, a "
        "branch, fork.json target_ms_id) — only a dev milestone is forkable; a "
        "sales Opportunity has no git worktree. Reading milestones is exact, not "
        "coupling. Independent AX review 2026-08-03 corrected an initial "
        "over-eager remediation of this site.",
}


def is_known_collection_coupling(verb: str, collection: str) -> bool:
    """True when (verb, collection) is an accepted-pending coupling in the
    ratchet allowlist (reported as debt, not a CI failure)."""
    return (verb, collection) in KNOWN_COLLECTION_COUPLING


def is_reviewed_legitimate_read(verb: str, collection: str) -> bool:
    """True when (verb, collection) is a human-reviewed LEGITIMATE read — the
    sought data lives only in that collection by design, so it is not profession
    coupling and must not be remediated (ms-134 e-4737)."""
    return (verb, collection) in REVIEWED_LEGITIMATE_COLLECTION_READS


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
    "doctor": "L0", "skill": "L0", "migrate": "L0",
    "reset": "L0", "update": "L0", "project": "L0",
    # L1 — all-profession coordination substrate (target-agnostic).
    "auth": "L1", "bus": "L1", "channel": "L1", "cloud": "L1", "cycle": "L1",
    "disclose": "L1", "undisclose": "L1", "dm": "L1", "help": "L1",
    "incident": "L1", "init": "L1", "member": "L1", "note": "L1",
    "onboarding": "L1", "operation": "L1", "org": "L1", "resume": "L1",
    "run": "L1", "search": "L1", "session": "L1", "sessions": "L1",
    "stop": "L1", "trek": "L1", "trigger": "L1",
    # reclassified 2026-08-03 (e-4737 台帳 review) — mis-scoped by noun NAME, not
    # behaviour; still this same L1 section (one header), rationale inline:
    "master": "L1",   # was L0: customer-identity master-sync drain (ms-111)
    "morning": "L1",  # was L3-sales: bus autonomous-activity summary (ms-55)
    "profile": "L1",  # was L3-sales: Beacon auth/backend profile listing (ms-64)
    # L2 — class-abstraction: operate on a Target via the abstraction.
    "claim": "L2", "doc": "L2", "review": "L2", "status": "L2",
    "summary": "L2", "target": "L2",
    # L3 — profession default (dev).
    "milestone": "L3", "task": "L3", "log": "L3", "save": "L3", "sync": "L3",
    "push": "L3", "deploy": "L3", "pr": "L3", "issue": "L3", "retro": "L3",
    "rollback": "L3", "entry": "L3", "stuck": "L3",
    # scenario (ms-136 自動デバッグ基盤): 公開配布される dev 一般機能 — dev
    # ユーザーが自プロジェクトの SPEC を検証する(pr/deploy/retro と同じ dev の
    # L3)。Beacon 運用側だけの非公開ツール(doctor/migrate=L0)ではない。concrete
    # 到達可否は L1/L2 を除外するだけで L0/L3 を分けない;分ける軸は『公開配布か』。
    "scenario": "L3",
    # L3 — profession default (sales). "watch" = the sales reply-watch
    # (sales_entities.set_watch — watch a thread for a reply at a cadence, ms-107);
    # it was mis-scoped L1 by its generic noun (reclassified 2026-08-03, e-4737).
    "account": "L3", "acquisition": "L3", "activity": "L3",
    "communication": "L3", "meeting": "L3", "nurturing": "L3",
    "opportunity": "L3", "phase": "L3", "sales": "L3", "contact": "L3",
    "dossier": "L3", "watch": "L3",
}

# Per-verb overrides where a single verb does not follow its noun's scope.
_VERB_SCOPE_OVERRIDE: dict = {
    # (none yet — the noun rules cover every current verb. Add here when a verb's
    # scope diverges from its noun, e.g. a future ``bus_*`` verb that is admin-only.)
}


def _noun_from_key(cap_key: str) -> str:
    """The noun of a capability key — the substring before the first ``_``
    (``task_done`` → ``task``). The ONE place that knows the key→noun split, so
    ``scope_of`` and ``owner_of`` cannot fork on the format (maintainability
    review 2026-08-03)."""
    return (cap_key or "").strip().split("_", 1)[0]


def scope_of(cap_key: str) -> str:
    """Return the L0..L4 scope of a capability (CLI verb dispatch key), or ``""``
    when its noun is unknown to the rules (surfaced by ``reconcile`` as
    unclassified). A per-verb override wins over the noun rule."""
    key = (cap_key or "").strip()
    if key in _VERB_SCOPE_OVERRIDE:
        return _VERB_SCOPE_OVERRIDE[key]
    return _NOUN_SCOPE.get(_noun_from_key(key), "")


def origin_of(cap_key: str) -> str:
    """Return a capability's origin. Everything shipped in this repo is
    ``beacon-default``; ``client-custom`` / ``individual-via-trailnode`` are
    stamped when a capability enters via a client project or TrailNode (the
    orthogonal authorship axis, ms-134 設計方針2). Kept as a function so the
    origin source can move to per-capability data without changing callers."""
    return "beacon-default"


# ---------------------------------------------------------------------------
# Ownership axis (ms-134 e-4738) — WHO a profession-specific capability belongs
# to. Scope says HOW WIDELY a capability may be shared (L0..L4); it does not say
# to WHICH profession an L3 default belongs, nor to WHICH project an L4
# capability belongs. That "belongs-to" is the prerequisite data for the
# distribution story (TrailNode org/profession packaging, ms-53/58): to ship
# "the sales defaults" or "project X's L4 capabilities" you must first know
# which capability is whose. Until e-4738 the L3 dev/sales split lived only in
# _NOUN_SCOPE comments ("documentation, not enforcement"); this turns it into
# enforced data.
#
# Ownership is ORTHOGONAL to both scope and origin:
#   - L0/L1/L2 are profession-SHARED — they have NO single owner (owner_of == "";
#     that is the correct state, not a missing classification).
#   - L3 is owned by exactly one PROFESSION (dev / sales / backoffice …).
#   - L4 is owned by exactly one PROJECT (none ship by default → registry empty).
# The checker enforces: every live L3 capability resolves to a profession, every
# live L4 to a project. A new L3 noun added to _NOUN_SCOPE without an owner here
# is surfaced (the same drift-catch as scope coverage).
# ---------------------------------------------------------------------------

# Recognised profession owners for L3 defaults — the validation allowlist that
# owner values are checked against. Kept as data (not a bool split) so its
# CONDITIONAL logic needs no change for a new profession. NOTE: ``backoffice`` is
# already listed but has no live capability yet. Registering an actual backoffice
# L3 capability still requires (all in THIS file):
#   1. verbs — a noun entry in ``_L3_NOUN_PROFESSION`` (noun → "backoffice"),
#   2. skills — a scope rule in ``_SKILL_SCOPE`` / ``_SKILL_PREFIX_SCOPE`` AND an
#      owner rule in ``_SKILL_OWNER`` / ``_SKILL_OWNER_PREFIX``.
# The checker catches a miss at CI time; this list is the authoring-time signpost
# (AX/maintainability review 2026-08-03).
PROFESSIONS = {"dev", "sales", "backoffice"}

# L3 noun -> owning profession. MUST stay in sync with the L3 entries of
# _NOUN_SCOPE: a test asserts every L3 noun has exactly one profession here and
# no entry here is stale (points at a non-L3 noun). Mirrors the dev/sales split
# the _NOUN_SCOPE comments already document, now machine-enforced.
_L3_NOUN_PROFESSION = {
    # dev profession defaults.
    "milestone": "dev", "task": "dev", "log": "dev", "save": "dev",
    "sync": "dev", "push": "dev", "deploy": "dev", "pr": "dev",
    "issue": "dev", "retro": "dev", "rollback": "dev", "entry": "dev",
    "stuck": "dev", "scenario": "dev",  # ms-136 自動デバッグ基盤 (dev L3)
    # sales profession defaults. NOTE: "morning" / "profile" were here until
    # 2026-08-03; they were reclassified L3-sales → L1 in _NOUN_SCOPE (e-4737,
    # they are bus/auth infra not sales) so they are INTENTIONALLY absent — the
    # sync test enforces this, do not re-add them. "watch" was added here (moved
    # L1 → L3-sales, the sales reply-watch).
    "account": "sales", "acquisition": "sales", "activity": "sales",
    "communication": "sales", "meeting": "sales", "nurturing": "sales",
    "opportunity": "sales", "phase": "sales", "sales": "sales",
    "contact": "sales", "dossier": "sales", "watch": "sales",
}

# L4 verb -> owning project. Empty: no L4 (project-only) capability ships in the
# product (by_scope L4 == 0). A project-local capability entering via a client
# project / TrailNode stamps its project id here (orthogonal to origin_of).
_L4_VERB_PROJECT: dict = {}


def owner_of(cap_key: str) -> str:
    """Return the ownership handle of a capability, dispatched by its scope
    (ms-134 e-4738):

      - L3 → the owning PROFESSION (``dev`` / ``sales`` / …), or ``""`` when the
        noun has no profession registered (surfaced by ``reconcile_ownership``).
      - L4 → the owning PROJECT id, or ``""`` when unregistered.
      - L0 (product-operation) / L1 / L2 (profession-shared) → ``""`` — none of
        these has a single owner. This is a CORRECT empty, distinguished from a
        missing one by scope: call ``owner_required(scope_of(cap))`` to tell
        "no owner is expected" from "an owner is missing". (Only L1/L2 are
        ``is_profession_shared``; L0 is product-operation — all three are simply
        not owner-bearing.)
    """
    scope = scope_of(cap_key)
    if scope == "L3":
        return _L3_NOUN_PROFESSION.get(_noun_from_key(cap_key), "")
    if scope == "L4":
        return _L4_VERB_PROJECT.get((cap_key or "").strip(), "")
    return ""


def owner_required(scope: str) -> bool:
    """True when a capability at ``scope`` must resolve to an owner (L3 → a
    profession, L4 → a project). L0/L1/L2 are shared and require none."""
    return scope in ("L3", "L4")


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


def reconcile_ownership(live: Optional[set] = None) -> dict:
    """Reconcile the OWNERSHIP axis (ms-134 e-4738) against the live CLI surface.
    Returns ``{"unowned": [...], "by_owner": {...}}``:

    - ``unowned``: live verbs whose scope REQUIRES an owner (L3 → profession,
      L4 → project) but resolves to none — an L3 capability under a new noun that
      was given a scope but no profession, or an L4 with no project. This is the
      OWNERSHIP-axis parallel of ``reconcile``'s ``unclassified`` (the scope-axis
      gap list); the keys differ because each names its own axis' gap.
    - ``by_owner``: count of live verbs per resolved owner, pre-seeded with every
      profession (so ``backoffice`` shows ``0`` before its first live verb rather
      than being absent) plus ``"(shared)"`` (L0/L1/L2) and ``"(unowned)"``.
    """
    live = enumerate_live_verbs() if live is None else live
    unowned = sorted(v for v in live
                     if owner_required(scope_of(v)) and not owner_of(v))
    by_owner: dict = {p: 0 for p in PROFESSIONS}
    by_owner["(shared)"] = 0
    by_owner["(unowned)"] = 0
    for v in live:
        if owner_required(scope_of(v)):
            key = owner_of(v) or "(unowned)"
        else:
            key = "(shared)"
        by_owner[key] = by_owner.get(key, 0) + 1
    return {"unowned": unowned, "by_owner": by_owner}


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
    "beacon-scenario-gen": "L3",  # ms-136: 自動デバッグ基盤の生成器 (dev L3, 公開配布)
    "beacon-incident-report": "L1", "beacon-init": "L1", "beacon-log": "L3",
    "beacon-map": "L2", "beacon-member": "L1", "beacon-note": "L1",
    "beacon-onboard": "L1", "beacon-pr-create": "L3", "beacon-push": "L3",
    "beacon-retro": "L2", "beacon-retrospect": "L2", "beacon-roadmap": "L2",
    "beacon-spec": "L2", "beacon-task": "L3", "beacon-vision": "L2",
    # L4 — project-local to the Beacon source repo itself. beacon-scope-classify
    # (ms-134 e-4739) edits THIS ledger (lib/capability_ledger.py); a pip-installed
    # Beacon user has no ledger to maintain, so it is useful only in the Beacon
    # repo = project-individual (L4). Its own layer was set through e-4739's own
    # propose→confirm ritual (AI proposed L4, user confirmed 2026-08-03) — the
    # mechanism dogfoods itself. This is the ledger's "a project-local skill would
    # be L4" example made concrete (the first live L4 capability).
    "beacon-scope-classify": "L4",
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


# L3 skill -> owning profession (ms-134 e-4738). sales skills via the
# ``beacon-sales-`` prefix; the dev delivery-workflow skills (all L3 in
# _SKILL_SCOPE) by exact name. A test asserts every L3 skill resolves here.
_SKILL_OWNER_PREFIX = (("beacon-sales-", "sales"),)
_SKILL_OWNER = {
    "beacon-deploy": "dev", "beacon-log": "dev", "beacon-pr-create": "dev",
    "beacon-push": "dev", "beacon-task": "dev", "beacon-scenario-gen": "dev",
}

# L4 skill -> owning PROJECT id (ms-134 e-4739), the skill-side parallel of
# ``_L4_VERB_PROJECT``. L4's owner is a project (not a profession): a project-local
# skill belongs to exactly one project. ``beacon`` = the Beacon source repo itself.
_SKILL_PROJECT = {
    "beacon-scope-classify": "beacon",
}


def skill_owner_of(skill_name: str) -> str:
    """Return the ownership handle of a Skill, dispatched by its scope (mirrors
    ``owner_of`` for verbs, ms-134 e-4738/e-4739):

      - L3 → the owning PROFESSION (``dev`` / ``sales`` / …), or ``""`` when
        unregistered.
      - L4 → the owning PROJECT id, or ``""`` when unregistered.
      - L0/L1/L2 (shared) → ``""`` — no single owner (a correct empty).
    """
    name = (skill_name or "").strip()
    scope = skill_scope_of(name)
    if scope == "L4":
        return _SKILL_PROJECT.get(name, "")
    if scope != "L3":
        return ""
    if name in _SKILL_OWNER:
        return _SKILL_OWNER[name]
    for prefix, owner in _SKILL_OWNER_PREFIX:
        if name.startswith(prefix):
            return owner
    return ""


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


def reconcile_skills_ownership(skills_dir: str = "") -> dict:
    """Reconcile the OWNERSHIP axis for skills against the live skill surface
    (ms-134 e-4738). Returns ``{"unowned": [...], "by_owner": {...}}`` — an L3
    skill with no profession owner is surfaced (mirrors ``reconcile_ownership``
    for verbs). Named ``reconcile_skills_ownership`` to parallel the scope-axis
    ``reconcile`` → ``reconcile_skills`` step (AX review 2026-08-03). ``by_owner``
    is pre-seeded with every profession, ``"(shared)"`` and ``"(unowned)"`` like
    the verb reconciler."""
    skills = enumerate_skills(skills_dir)
    unowned = sorted(s for s in skills
                     if owner_required(skill_scope_of(s)) and not skill_owner_of(s))
    by_owner: dict = {p: 0 for p in PROFESSIONS}
    by_owner["(shared)"] = 0
    by_owner["(unowned)"] = 0
    for s in skills:
        if owner_required(skill_scope_of(s)):
            key = skill_owner_of(s) or "(unowned)"
        else:
            key = "(shared)"
        by_owner[key] = by_owner.get(key, 0) + 1
    return {"unowned": unowned, "by_owner": by_owner}


# ---------------------------------------------------------------------------
# Classification PROPOSAL (ms-134 e-4739) — the authoring-time forcing function.
#
# reconcile* above DETECT a gap (a new noun/skill with no scope, an L3/L4 with no
# owner); the CI checker fails on it. But detection only says "UNCLASSIFIED" — it
# leaves the human to derive the layer AND lets an AI self-classify silently. This
# turns detection into a structured PROPOSAL: for every gap, emit the exact ledger
# edit site, the L0..L4 menu, and a best-effort guess with a confidence — so the
# classification of a new capability is proposed structurally (not by a prompt's
# please-remember), and the actual write goes through a human-confirm gate in the
# /beacon-scope-classify Skill (AI proposes → human confirms → ledger written),
# never a silent AI edit. Mirrors the attainment verdict's propose→confirm split.
#
# This function is PURE (enumerate + transform, no I/O beyond the surface reads
# reconcile* already do, no AST): the guess is deliberately conservative — a
# confident scope/owner only when a real token signal exists (a profession name in
# the capability's own name). For a bare new verb noun there is NO signal, so the
# guess is empty and the human decides against the menu. The proposal's value is
# the STRUCTURE (the gap, its exact edit site, the menu), not a fabricated layer;
# the deeper reasoning (reading the handler to tell an L3 default from an L1/L2
# leak) is the Skill's AI job on top of this scaffold.
# ---------------------------------------------------------------------------

def _profession_token(name: str) -> str:
    """Return the profession named as a token in ``name`` (``beacon-sales-email``
    → ``sales``; ``payroll_run`` → ``""``), or ``""`` when none. The one real
    signal a pure guess can trust: a capability that names its profession in its
    own identifier almost certainly belongs to that profession (L3)."""
    tokens = set((name or "").replace("_", "-").split("-"))
    for p in sorted(PROFESSIONS):
        if p in tokens:
            return p
    return ""


def _scope_edit_sites(kind: str, capability: str, noun: str, scope: str) -> list:
    """The exact ledger edit(s) that give ``capability`` a SCOPE. ``scope`` is the
    guessed level ("" when unguessed — the edit still names the site with a
    ``<scope>`` placeholder so the human fills it). Conditional owner follow-ups
    (L3 → profession, L4 → project) travel as ``note`` so one proposal carries the
    whole "what to edit" story."""
    value = scope or "<L0|L1|L2|L3|L4>"
    if kind == "skill":
        return [{
            "file": "lib/capability_ledger.py",
            "dict": "_SKILL_SCOPE",
            "key": capability,
            "value_hint": value,
            "note": ("or add a longest-wins rule to _SKILL_PREFIX_SCOPE if this "
                     "introduces a family (e.g. ('beacon-<prof>-', 'L3')). "
                     "If L3, ALSO set the owner: _SKILL_OWNER[name] or a "
                     "_SKILL_OWNER_PREFIX tuple; if L4, ALSO set "
                     "_SKILL_PROJECT[name]=<project-id>."),
        }]
    return [{
        "file": "lib/capability_ledger.py",
        "dict": "_NOUN_SCOPE",
        "key": noun,
        "value_hint": value,
        "note": ("classifies EVERY verb under this noun. A per-verb exception "
                 "goes in _VERB_SCOPE_OVERRIDE instead. If L3, ALSO add "
                 "_L3_NOUN_PROFESSION[noun]=<profession>; if L4, "
                 "_L4_VERB_PROJECT[verb]=<project>."),
    }]


def _owner_edit_sites(kind: str, capability: str, noun: str, scope: str) -> list:
    """The exact ledger edit(s) that give an L3/L4 ``capability`` an OWNER (scope
    already known, only the owner is missing)."""
    if scope == "L4":
        if kind == "skill":
            return [{"file": "lib/capability_ledger.py", "dict": "_SKILL_PROJECT",
                     "key": capability, "value_hint": "<project-id>",
                     "note": "L4 skill belongs to exactly one project."}]
        return [{"file": "lib/capability_ledger.py", "dict": "_L4_VERB_PROJECT",
                 "key": capability, "value_hint": "<project-id>",
                 "note": "L4 verb belongs to exactly one project."}]
    # L3
    if kind == "skill":
        return [{"file": "lib/capability_ledger.py", "dict": "_SKILL_OWNER",
                 "key": capability, "value_hint": "<profession>",
                 "note": ("or a _SKILL_OWNER_PREFIX tuple if this is a new "
                          "profession family (e.g. ('beacon-<prof>-', '<prof>')).")}]
    return [{"file": "lib/capability_ledger.py", "dict": "_L3_NOUN_PROFESSION",
             "key": noun, "value_hint": "<profession>",
             "note": "add the profession to PROFESSIONS first if it is new."}]


def propose(live: Optional[set] = None, skills_dir: str = "") -> dict:
    """Return a structured classification PROPOSAL for every open gap (ms-134
    e-4739). Read-only: it never writes the ledger — the write is the
    /beacon-scope-classify Skill's human-confirmed step.

    Returns ``{"ok", "gap_count", "proposals", "scope_menu", "owner_menu",
    "scanned"}``:
    - ``ok``: no gaps (nothing to classify). This is the field an exit-code-driven
      caller must branch on — ``--propose`` ALWAYS exits 0 (it is advisory, not a
      gate), so read ``ok`` (``false`` = gaps exist), never ``$?`` (AX review 581).
    - ``gap_count``: number of ``proposals``.
    - ``proposals``: one dict per gap, each with ``kind`` (verb/skill), ``gap``
      (scope/owner), the capability, a best-effort ``proposed_scope`` /
      ``proposed_owner`` (``""`` when there is no signal), a ``confidence``
      (``high`` only on a profession-token signal, else ``low``), a ``rationale``,
      and ``edits`` — the exact ledger site(s) to change. Ordered verb-scope,
      verb-owner, skill-scope, skill-owner for a stable read. Each edit's
      ``value_hint`` is the value to WRITE when confident (e.g. ``"L3"``), or an
      angle-bracket PLACEHOLDER (``"<L0|L1|L2|L3|L4>"`` / ``"<project-id>"``) that
      the human substitutes — never write a ``<...>`` string verbatim (AX review 581).
    - ``scope_menu``: the L0..L4 levels the human chooses a scope from.
    - ``owner_menu``: the PROFESSION list, for an L3 owner ONLY. An L4 owner is a
      PROJECT id (a free string, no fixed list) — take it from the gap's edit
      ``note`` / ``value_hint`` (``_SKILL_PROJECT`` / ``_L4_VERB_PROJECT``), not
      from this menu (AX review 581: the menu is L3-only).
    - ``scanned``: ``{"verbs": N, "skills": M}`` — how many live capabilities were
      inspected, so an ``ok``/empty result is distinguishable from "the scanner
      looked in the wrong place and found nothing" (AX review 581).
    """
    live = enumerate_live_verbs() if live is None else live
    verb_scope_gaps = reconcile(live)["unclassified"]
    verb_owner_gaps = reconcile_ownership(live)["unowned"]
    skill_scope_gaps = reconcile_skills(skills_dir)["unclassified"]
    skill_owner_gaps = reconcile_skills_ownership(skills_dir)["unowned"]

    proposals = []

    for verb in verb_scope_gaps:
        noun = _noun_from_key(verb)
        prof = _profession_token(verb)
        proposals.append({
            "capability": verb, "kind": "verb", "gap": "scope", "noun": noun,
            "known_scope": "",
            "proposed_scope": "L3" if prof else "",
            "proposed_owner": prof,
            "confidence": "high" if prof else "low",
            "rationale": (
                f"noun '{noun}' names profession '{prof}' → L3/{prof} default."
                if prof else
                f"new noun '{noun}' — no scope signal from the name. Decide "
                f"against the menu: is it product-ops (L0), shared substrate (L1), "
                f"a target-abstraction rule (L2), a profession default (L3), or "
                f"one project's (L4)? Reading the handler settles it."),
            "edits": _scope_edit_sites("verb", verb, noun, "L3" if prof else ""),
        })

    for verb in verb_owner_gaps:
        noun = _noun_from_key(verb)
        scope = scope_of(verb)
        prof = _profession_token(verb)
        proposals.append({
            "capability": verb, "kind": "verb", "gap": "owner", "noun": noun,
            "known_scope": scope,
            "proposed_scope": scope,
            "proposed_owner": prof,
            "confidence": "high" if prof else "low",
            "rationale": (
                f"{scope} verb needs an owner; name names '{prof}'."
                if prof else
                f"{scope} capability requires an owner "
                f"({'profession' if scope == 'L3' else 'project'}) — pick from the "
                f"menu; the noun gives no signal."),
            "edits": _owner_edit_sites("verb", verb, noun, scope),
        })

    for skill in skill_scope_gaps:
        prof = _profession_token(skill)
        proposals.append({
            "capability": skill, "kind": "skill", "gap": "scope", "noun": "",
            "known_scope": "",
            "proposed_scope": "L3" if prof else "",
            "proposed_owner": prof,
            "confidence": "high" if prof else "low",
            "rationale": (
                f"skill name contains profession token '{prof}' → L3/{prof}."
                if prof else
                "no profession token — likely L1 (coordination substrate) or L2 "
                "(operates on the shared Target abstraction); decide by what CLI "
                "verbs it drives."),
            "edits": _scope_edit_sites("skill", skill, "", "L3" if prof else ""),
        })

    for skill in skill_owner_gaps:
        scope = skill_scope_of(skill)
        prof = _profession_token(skill)
        proposals.append({
            "capability": skill, "kind": "skill", "gap": "owner", "noun": "",
            "known_scope": scope,
            "proposed_scope": scope,
            "proposed_owner": prof,
            "confidence": "high" if prof else "low",
            "rationale": (
                f"{scope} skill needs an owner; name names '{prof}'."
                if prof else
                f"{scope} skill requires an owner "
                f"({'profession' if scope == 'L3' else 'project'}); no name signal."),
            "edits": _owner_edit_sites("skill", skill, "", scope),
        })

    return {
        "ok": not proposals,
        "gap_count": len(proposals),
        "proposals": proposals,
        "scope_menu": dict(SCOPE_LEVELS),
        "owner_menu": sorted(PROFESSIONS),
        "scanned": {"verbs": len(live),
                    "skills": len(enumerate_skills(skills_dir))},
    }
