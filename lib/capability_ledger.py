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

from typing import Optional

import cli_surface


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

# Sharing breadth for the dependency partial order (bigger = shared more widely).
# L0 is intentionally absent: it is the product-operation layer, a separate axis.
# Its rule is expressed directly in ``may_depend`` / the checker, not as a breadth
# rank — an L0 capability may depend on L1/L2 but must not be depended ON by a
# public (L1..L4) capability, and must not ship in the public distribution.
_SCOPE_BREADTH = {"L1": 4, "L2": 3, "L3": 2, "L4": 1}

# Profession-shared scopes: capabilities here must not reach a profession concrete.
PROFESSION_SHARED_SCOPES = ("L1", "L2")

# The profession-specific concrete symbols an L1/L2 capability must NOT call.
# Each maps to the abstraction it should use instead (surfaced in the violation
# message so a fix is one step away, per ms-115 方針5).
PROFESSION_CONCRETE_SYMBOLS = {
    "core.save_entry":
        "dev milestone changelog recorder — use occupation.record_target_entry",
    "core.find_target_milestone":
        "dev milestone resolver — record via occupation.record_target_entry",
}


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
