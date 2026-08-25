"""Beacon target STATE model — the occupation-agnostic declaration of "how a
Target advances its phase/state", plus the ``set_target_state`` primitive that
moves any target-class through its declared non-terminal transitions on ONE
path (ms-142 e-5157 / T2, class-engine ideal §2/§5 "フェイズ前進").

WHY this module exists
----------------------
Before T2, every target-class carried its state-advancement logic in a
different place and shape:

  * milestone — a permissive ``status`` enum (``core.VALID_STATUSES``), advanced
    by ``milestone_start`` / ``milestone_wait`` / ``milestone_update``; its
    completion (→ done / observing) is guarded by the ms-119 目的達成 review gate.
  * operation — a monotonic ``status`` lifecycle table
    (``core.LIFECYCLE_TRANSITIONS['operation']``), advanced by
    ``operation_set_status`` and closed by ``operation_close``.
  * opportunity — a CONFIG-derived funnel of ``phase`` values plus a
    ``who_has_the_ball``, advanced by ``sales_entities.phase_set`` (permissive:
    the human is the master, no table); its terminal is a phase whose config
    outcome is won/lost.
  * acquisition — a monotonic ``status`` table
    (``core.LIFECYCLE_TRANSITIONS['acquisition']``), advanced by
    ``acquisition_set_status``; terminal ``done`` via ``work_model.mark_done``.
  * descriptor-defined classes — ordered ``phases`` with a ``terminal`` flag and
    a ball, already advanced generically by ``target_engine.advance_target`` /
    ``close_target``.

Four shapes, five write paths, no single place a reader could ask "what states
does this class have, which are terminal, and how do I move it one step". That
missing declaration is why ``phase_ball`` had to be HARDCODED as a standalone
dict in ``occupation.py`` (``_ARM_PHASE_BALL``): with no state model to read it
from, the projection was wired by hand. This module makes the state model
first-class DATA so ``occupation.profession_manifest`` DERIVES ``phase_ball``
from it (the hardwire is dissolved) and a follow-up can route each class's
advance verb through ``set_target_state``.

SCOPE of T2 (leader Q2 段階化)
-----------------------------
This module DECLARES all four built-in state models + derives descriptor ones,
and ``set_target_state`` is the single non-terminal advance path for all classes:
the three status/table classes (milestone / operation / acquisition), descriptor
phases (delegated to the proven ``target_engine``), and — since ms-142 e-5169 —
the opportunity FUNNEL (delegated to the sales seam ``sales_entities.
advance_funnel_phase``, which owns the phase↔status mirror). The former HOT-PATH
verbs now ride this one path: ``sales_entities.phase_set`` routes a non-terminal
opportunity phase through ``set_target_state`` (the status mirror is invariant),
and the milestone advance verbs ``core.milestone_start`` / ``milestone_wait``
write ``in_progress`` / ``waiting`` through it. The terminal/gated writes stay on
their class verbs (the review gate for a milestone, the sales judge gate for an
opportunity settlement) — set_target_state refuses those by contract.

COMPLETION-GATE NON-BYPASS (leader caution 1, the most important invariant)
--------------------------------------------------------------------------
``set_target_state`` writes ONLY non-terminal transitions. Every terminal /
gate-managed state (milestone done/observing/cancelled/in_review/approved,
operation closed, acquisition done/cancelled, a descriptor terminal phase) is
REFUSED and the error names the class's existing terminal verb to route through.
So it is STRUCTURALLY impossible for ``set_target_state`` to land a milestone on
done/observing behind the review gate — the guard is the shape of the primitive,
not a prompt. ``tests/test_target_state.py`` pins this.

Like ``work_model`` / ``target_engine`` this module performs no I/O: every
function is a pure transform over the ``data`` dict it is handed; persistence is
the CLI layer's job. Concrete occupation modules (``core`` for the monotonic
guard, ``occupation`` for record resolution) are imported LAZILY inside the
functions that need them, so importing this module never forms a cycle
(``occupation`` eager-imports this for the ``phase_ball`` derivation).
"""

from __future__ import annotations

from typing import Optional

import work_base
import work_model as _wm


class TargetStateError(ValueError):
    """Raised when a generic state transition cannot proceed (unknown target,
    unknown state, or a terminal/gated transition that must route through a
    class verb). Subclasses ``ValueError`` so callers catching ``ValueError``
    keep working; carries a human-facing message the CLI prints."""


# ---------------------------------------------------------------------------
# State-model shapes — the four forms a target-class state model can take
# (leader Option A). A shape tells a reader HOW the states are structured, so a
# consumer (the manifest projection, a future generic advance verb) can reason
# about a class without a per-kind branch.
# ---------------------------------------------------------------------------

SHAPE_STATUS_ENUM = "status_enum"          # milestone: permissive status enum
SHAPE_TRANSITION_TABLE = "transition_table"  # operation / acquisition: monotonic
SHAPE_FUNNEL = "funnel"                     # opportunity: config-derived phases + ball
SHAPE_PHASES = "phases"                     # descriptor: ordered phases + terminal + ball


# ---------------------------------------------------------------------------
# Completion-gate projections (ms-142 T3 / e-5158). Every target-class's terminal
# transition MUST pass through a completion gate — the anti-self-close capability
# whose EXISTENCE must never leak when a new occupation is declared (class-engine
# ideal §5: "ゲートの存在は漏らすな（中身ロジックは L3 可）"). A class declares
# WHICH gate projection guards its terminal; the internal logic stays L3 (each
# projection differs), but the DECLARATION is uniform so the coverage matrix (T5)
# can check "every class with a terminal declares a gate" from one field.
#
# The three projections that exist today (leader Q2 ruling: Scope B — the two
# ungated classes get the lightweight structural ban, not the full spine):
GATE_SPINE = "spine"                # dev spine: transition_approval + beacon target
                                    # review-request/approve (milestone / operation).
GATE_SALES_JUDGE = "sales-judge"    # sales judge flow: opportunity_judge (the human
                                    # verdict against meeting evidence).
GATE_SELF_CLOSE_BAN = "self-close-ban"  # lightweight structural gate: the terminal
                                    # verb refuses an AI session's direct completion
                                    # (BEACON_TARGET_COMPLETE_USER_OVERRIDE / human
                                    # session bypass) — the gate EXISTS, its criteria
                                    # stay L3 (acquisition / descriptor classes).
#
# ⚠ DECLARATION ≠ ENFORCEMENT (ms-142 T3 maintainability review): declaring
# ``completion_gate`` on a state model is a LABEL — it does NOT auto-wire the ban.
# Enforcement lives in each class's terminal VERB (``cmd_acquisition status`` /
# ``beacon target close`` apply the self-close ban; milestone/operation route the
# spine; opportunity_judge is the sales gate). The descriptor path is generic
# (``cmd_target close`` covers every descriptor kind), but a NEW BUILT-IN class
# added here with ``completion_gate`` set must ALSO have its verb wired to the gate.
# The T5 coverage matrix is the drift-checker that fails when a class declares a
# gate its terminal path does not actually apply — until it lands, keep the two in
# sync by hand.


# ---------------------------------------------------------------------------
# Built-in state models — declarative data, keyed by occupation-agnostic KIND
# (``milestone`` / ``operation`` / ...). NOTE the key is the kind, NOT the
# collection: the sibling registries in ``occupation.py`` (``_ARM_ROLES`` /
# ``_COLLECTION_KIND``) are collection-keyed (``milestones``), so a new built-in
# added here must be keyed by its kind and ``profession_manifest`` bridges the two
# via ``_collection_kind`` (ms-142 T2 maintainability review: flag the two keying
# schemes so a future edit does not key a state model by collection and read back
# ``None``).
#
# Each model carries:
#   shape             — one of the SHAPE_* constants.
#   state_field       — the record field holding the state (``status`` or ``phase``).
#   advanceable_states— the non-terminal states ``set_target_state`` may WRITE
#                       generically (None = config-derived / deferred, funnel).
#   routed_states     — {state -> the class verb that must be used to reach it};
#                       these are REFUSED by set_target_state. This is where the
#                       completion-gate non-bypass lives: a milestone's terminal
#                       and gate-managed states point at the --review verbs.
#   ball_field        — the record field holding who-has-the-ball, or None for a
#                       class with no ball (milestone / operation / acquisition).
#   monotonic         — True → the non-terminal move is validated against
#                       ``core.LIFECYCLE_TRANSITIONS`` (the SSOT table); False →
#                       permissive (the human is master).
#   phases_ref        — for a funnel, the config accessor its phases come from.
#   completion_gate   — which gate projection guards the terminal transition
#                       (GATE_SPINE / GATE_SALES_JUDGE / GATE_SELF_CLOSE_BAN), or
#                       None for a never_terminal class; the anti-self-close
#                       capability's declared existence (T3).
#   never_terminal    — UNIVERSAL slot (every model declares it, ms-142 e-5256):
#                       True = the class never settles (no 決着 grain, e.g. an
#                       account's 継続 relationship → completion_gate is a declared
#                       None); False = it settles. Invariant: completion_gate is
#                       non-null XOR never_terminal. The coverage matrix's
#                       completion-gate N/A predicate reads never_terminal.
#   funnel_seam       — SHAPE_FUNNEL slot (EVERY funnel model MUST declare it —
#                       ``test_every_funnel_declares_funnel_seam`` pins that, so an
#                       OMIT is never mistaken for a None-DECLARATION; the anti-blind-
#                       spot rule this PR is about). Names the generic set_target_state
#                       advance seam (any non-None id), or None when the funnel's phase
#                       is written by a class verb (account → ``acc- phase_set``). A
#                       NON-None value routes dispatch to ``_advance_funnel``; None
#                       routes to the class's ``phase_verb`` error. Non-funnel classes
#                       omit it (they never reach the SHAPE_FUNNEL branch).
#   phase_verb        — a SHAPE_FUNNEL-with-``funnel_seam=None`` model MUST declare it:
#                       the class-specific phase-change verb hint (a ``{target_id}``
#                       template) the generic set_target_state error interpolates, so a
#                       seam-less funnel is not mis-directed by a hardcoded string.
#
# The advanceable/routed split is the SINGLE source for "which states does a
# class own and which need a class verb". ``core.VALID_STATUSES`` /
# ``VALID_OPERATION_STATUSES`` / ``ACQUISITION_STATUSES`` and
# ``LIFECYCLE_TRANSITIONS`` remain the canonical VOCABULARY + monotonic guard;
# this model references them (monotonic=True → validate via the table) rather
# than duplicating the transitions, so the two cannot drift.
# ---------------------------------------------------------------------------

# BUILTIN_TARGET_CLASSES — the SINGLE SOURCE for every built-in Target class (ms-142
# e-5265). Before this, adding a 4th/5th class meant a 3-file / 6-site shotgun edit
# (``occupation.TARGET_COLLECTIONS`` / ``_ARM_ROLES`` / ``_COLLECTION_KIND`` +
# ``target_state.BUILTIN_STATE_MODELS``), and missing one silently drifted. Now each
# class is ONE entry here carrying BOTH its state model AND its cross-registry identity:
#   - ``collection``  — the project.json key holding the records;
#   - ``aggregatable`` — True ⇒ in the manifest / ``TARGET_COLLECTIONS`` (walked by
#     session_log / deadline / claim); acquisition is False (it rides a separate
#     persistence path — has a state model but is NOT a manifest collection);
#   - ``arm_roles``   — the work-item / evidence / changelog arm classification
#     (``None`` for a class with no arms, e.g. acquisition);
#   - the rest of the entry IS the state model (shape / state_field / routed_states /
#     …), verbatim as before.
# The four registries are DERIVED below + in ``occupation`` from this one declaration,
# so a new class's membership in THOSE FOUR is one append here — the drift they used to
# risk is now structurally impossible. SCOPE (e-5265 maint review): the other two Target
# lenses — ``occupation.TARGET_DECOMPOSITION`` (physical row decomposition, excludes
# operations, includes acquisitions) and ``claim_target_collections`` (adds acquisitions)
# — have DIFFERENT membership and are NOT derived here; a physically-decomposed class
# still needs its ``TARGET_DECOMPOSITION`` entry too (a 2-site add, not 1). Their honest
# ``⊇`` invariant still guards them. Lives in target_state (not occupation) because the state
# model — the bulky part — anchors on target_state's SHAPE_*/GATE_* constants; the
# arm_roles are inert DATA occupation reads (target_state itself never consumes them) —
# a documented pure-data tradeoff for the single-entry goal (a fully clean layering would
# hoist SHAPE_*/GATE_* + this master into a new lowest module, deferred as disproportionate
# churn; e-5265 maint review). ORDER MATTERS: the aggregatable entries' order IS
# ``TARGET_COLLECTIONS``' tuple order (milestones, opportunities, operations, accounts),
# pinned by ``test_occupation_descriptor.test_dev_project_unchanged`` (tuple) +
# ``test_occupation_manifest.test_registries_derive_from_single_source_e5265``. INSERT a
# new AGGREGATABLE class among the first four (before the non-aggregatable ``acquisition``
# entry) and UPDATE those tuple pins; a non-aggregatable class may go anywhere (it is
# filtered out of TARGET_COLLECTIONS). The validator below fails loud on a broken invariant.
BUILTIN_TARGET_CLASSES: dict[str, dict] = {
    "milestone": {
        "collection": "milestones",
        "aggregatable": True,
        "arm_roles": {
            # ms-143: ``id_prefix`` is the declarative work-item id prefix — a dev
            # task is ``e-`` (shared with operation entries, see next_entry_id).
            "work_item_arm": {"arm": "entries", "item_type": "task", "kind": "task",
                              "id_prefix": "e-"},
            "evidence_arms": [{"arm": "entries", "item_type": "commit"}],
            "changelog": {"arm": "entries", "recorder": "milestone"},
        },
        # ms-155 e-5598: the milestone class's DELIVERABLE (生み出した価値) is its
        # 機能投影 — the application-map (今このプロダクトに何ができるかを写した現在地
        # の索引, CORE doc ``application-map``). spine §2b names milestone→機能
        # (application-map) as the first per-class deliverable, and this declaration
        # RE-HOMES the existing map AS milestone's deliverable projection: the
        # ``"doc"`` projector says "the produced value IS the document named by
        # ``ref``". A registry-only slot (like ``arm_roles`` — inert data the state
        # model never consumes; listed in REGISTRY_ONLY_KEYS below). WHY the map's
        # long-standing ``profession==dev`` gate is the PRINCIPLE's manifestation,
        # not a special-case: the deliverable rides the milestone CLASS, and only a
        # dev project adopts the milestone class, so the map surfaces exactly for
        # dev — the gate emerges from class-adoption, there is no separate
        # ``if profession == 'dev'`` behind it (SPEC 受入条件3). The root union
        # (e-5599) collects this only when the project adopts milestone.
        "deliverable": {"kind": "feature-map", "label": "機能",
                        "projector": "doc", "ref": "application-map"},
        "kind": "milestone",
        "shape": SHAPE_STATUS_ENUM,
        "state_field": "status",
        # Routine work states set_target_state may write directly (no completion
        # claim, so the ms-119 gate does not apply to any of them).
        "advanceable_states": ("todo", "in_progress", "waiting"),
        # Terminal + gate-managed states — REFUSED by set_target_state. done /
        # observing are completion claims that MUST pass the 目的達成 review gate
        # (ms-119); cancelled is a delete; in_review / approved are set by the
        # review gate flow itself, not by a free transition.
        "routed_states": {
            "done": "beacon milestone done <id> --review (目的達成 gate; "
                    "AI assembles evidence, human approves)",
            "observing": "beacon milestone observe <id> --review "
                         "(目的達成 gate — observing は完了主張なので迂回不可)",
            "cancelled": "beacon milestone delete <id>",
            "in_review": "review gate (beacon milestone done/observe --review)",
            "approved": "review gate (human approval)",
        },
        "ball_field": None,
        "monotonic": False,
        "phases_ref": None,
        "completion_gate": GATE_SPINE,   # ms-119 目的達成 review + AI-direct ban
        "never_terminal": False,         # milestones settle (done/observing)
    },
    "opportunity": {
        "collection": "opportunities",
        "aggregatable": True,
        "arm_roles": {
            "work_item_arm": {"arm": "activities", "item_type": None,
                              "kind": "activity", "id_prefix": "act-"},
            "evidence_arms": [{"arm": "communications", "item_type": None}],
            "changelog": None,
        },
        "kind": "opportunity",
        "shape": SHAPE_FUNNEL,
        "state_field": "phase",
        # Funnel phases are CONFIG-derived (per project), so the advanceable set
        # is resolved at runtime (in the sales seam), not declared here. ms-142
        # e-5169: set_target_state now routes a NON-terminal funnel transition by
        # delegating to sales_entities.advance_funnel_phase (which owns the
        # phase↔status mirror); a terminal phase is refused (sales judge gate).
        "advanceable_states": None,
        "routed_states": {},
        "ball_field": "who_has_the_ball",
        "monotonic": False,
        "phases_ref": "opportunity_phases",
        # the existing sales judge flow (opportunity_judge) IS the gate; the spine
        # deliberately does not stack a second one (transition_approval docstring).
        "completion_gate": GATE_SALES_JUDGE,
        "never_terminal": False,         # opportunities settle (sales judge 決着)
        # ms-142 e-5256: this funnel HAS a generic set_target_state seam — a
        # non-terminal advance delegates to ``_advance_funnel`` (the sales seam that
        # writes phase + mirrored status). ``funnel_seam`` is a SHAPE_FUNNEL-only slot
        # (irrelevant to non-funnel classes, so they omit it): ONLY its presence gates
        # dispatch — set_target_state's funnel branch runs ``_advance_funnel`` when it
        # is non-None and routes to the class verb when None. The string value is a
        # human-readable seam id (any truthy id names the seam for a reader); a SIBLING
        # funnel WITHOUT a generic seam (account) declares ``None``, a distinction read
        # from the model, not a kind-branch.
        "funnel_seam": "sales-opportunity",
    },
    "operation": {
        "collection": "operations",
        "aggregatable": True,
        # operations: work_item_arm is a DECLARED None (T1 裁定 — OperationTasks keep
        # their own ``operation task done`` L3 path), evidence_arms empty; its
        # changelog records onto ``entries`` via the ``plain`` recorder (e-5255).
        "arm_roles": {"work_item_arm": None, "evidence_arms": [],
                      "changelog": {"arm": "entries", "recorder": "plain"}},
        "kind": "operation",
        "shape": SHAPE_TRANSITION_TABLE,
        "state_field": "status",
        "advanceable_states": ("todo", "in_progress", "open"),
        "routed_states": {"closed": "beacon operation close <id>"},
        "ball_field": None,
        "monotonic": True,   # validated via core.LIFECYCLE_TRANSITIONS['operation']
        "phases_ref": None,
        "completion_gate": GATE_SPINE,   # same dev spine as milestone (close)
        "never_terminal": False,         # operations settle (closed)
    },
    "account": {
        "collection": "accounts",
        "aggregatable": True,
        # ms-142 e-5256: an Account's planned work is its ``nurturings`` arm (継続関係の
        # 手入れ — every item is a work item, ``item_type`` None; ids ``nrt-``), and its
        # proof is the SAME ``communications`` arm the opportunity uses; no dev-era
        # changelog (its records ride the evidence arm via add_evidence, e-5255).
        "arm_roles": {
            "work_item_arm": {"arm": "nurturings", "item_type": None,
                              "kind": "nurturing", "id_prefix": "nrt-"},
            "evidence_arms": [{"arm": "communications", "item_type": None}],
            "changelog": None,
        },
        "kind": "account",
        "shape": SHAPE_FUNNEL,
        "state_field": "phase",
        # Account phases are CONFIG-derived (``account_phases``), like the
        # opportunity funnel — the advanceable set is resolved at runtime, not here.
        "advanceable_states": None,
        "routed_states": {},
        # ms-142 e-5256: an Account carries NO ball. It is a 継続 customer
        # relationship tracked by a phase ladder (未接触→リード→未成約顧客→成約顧客),
        # not a deal sitting in someone's court, so the record has no
        # ``who_has_the_ball`` field. A DECLARED absence, not a gap — so
        # ``derive_phase_ball`` yields None (no phase/ball pair), matching the data.
        "ball_field": None,
        "monotonic": False,
        "phases_ref": "account_phases",
        # ms-142 e-5256: an Account NEVER settles — ``DEFAULT_ACCOUNT_PHASES`` has no
        # terminal phase; the relationship is 継続. ``never_terminal`` is the
        # DECLARATIVE marker the coverage matrix's completion-gate N/A predicate
        # reads. It is GENERAL (not account-specific): any future never-terminal
        # class shares it, while a terminal class (milestone/operation/opportunity/
        # acquisition) is ``never_terminal=False`` → its completion gate must exist.
        "never_terminal": True,
        # No 決着 grain ⇒ no completion gate. A declared absence consistent with
        # ``never_terminal`` (the matrix asserts the behaviour agrees: nothing lights
        # up on the completion-gate cell).
        "completion_gate": None,
        # ms-142 e-5256: an Account's phase is written by the sales L3 verb
        # ``acc- phase_set`` (a direct phase write, no status mirror) and advanced UP
        # by derivation from its opportunities' outcomes
        # (``_auto_advance_account_phase``). It has NO generic set_target_state seam:
        # ``_advance_funnel`` is the opportunity status-mirror writer and must not run
        # on an Account (leader 裁定 e-5256). ``funnel_seam=None`` routes
        # set_target_state to the class's ``phase_verb`` error rather than the wrong
        # (opportunity) writer — declaratively (reads the model), not a kind-branch.
        "funnel_seam": None,
        # The verb hint the generic set_target_state error interpolates (a
        # ``{target_id}`` template). Declared per class so the error carries no
        # account-hardcoded string — a future seam-less funnel supplies its own.
        "phase_verb": "`beacon account phase {target_id} <phase>` で進めてください "
                      "(顧客フェーズは商談の成約からも自動 derive されます)。",
    },
    "acquisition": {
        # acquisition is NOT aggregatable — it has a state model but rides a separate
        # persistence path (not a manifest / TARGET_COLLECTIONS member, no arm_roles).
        "collection": "acquisitions",
        "aggregatable": False,
        "arm_roles": None,
        "kind": "acquisition",
        "shape": SHAPE_TRANSITION_TABLE,
        "state_field": "status",
        "advanceable_states": ("todo", "in_progress"),
        "routed_states": {
            "done": "beacon acquisition status <id> done "
                    "(terminal — stamps done_at via work_model.mark_done)",
            "cancelled": "beacon acquisition cancel <id> (soft-cancel)",
        },
        "ball_field": None,
        "monotonic": True,   # validated via core.LIFECYCLE_TRANSITIONS['acquisition']
        "phases_ref": None,
        # ms-142 T3: no gate existed; Scope B gives it the lightweight structural
        # ban (AI cannot self-close `done` without a human/override signal).
        "completion_gate": GATE_SELF_CLOSE_BAN,
        "never_terminal": False,         # acquisitions settle (done/cancelled)
    },
}

# The registry-identity keys each master entry carries BEYOND the state model itself
# (ms-142 e-5265). The state model IS the entry minus these three keys, so there is no
# second copy that can drift — BUILTIN_STATE_MODELS is DERIVED, not hand-kept. PUBLIC
# (e-5265 AX review): a consumer that needs to strip registry keys reads THIS, and the
# validator below guards a collision with a state-model field name. Prefer the public
# accessor ``state_model_for(data, kind)`` over stripping keys by hand.
# ms-155 e-5598: ``deliverable`` (生み出した価値の投影宣言) is registry-only data —
# like ``arm_roles``, the state model never consumes it, so it is STRIPPED here to
# keep BUILTIN_STATE_MODELS a pure state model (else it would leak in and the base-
# key derive-by-strip invariant would still hold, but the model would carry a
# non-state field). Only milestone declares one today; a class without it is
# unaffected.
REGISTRY_ONLY_KEYS = ("collection", "aggregatable", "arm_roles", "deliverable")
_REGISTRY_ONLY_KEYS = REGISTRY_ONLY_KEYS   # back-compat alias for internal callers

BUILTIN_STATE_MODELS: dict[str, dict] = {
    kind: {k: v for k, v in cls.items() if k not in REGISTRY_ONLY_KEYS}
    for kind, cls in BUILTIN_TARGET_CLASSES.items()
}

# The state-model keys EVERY class carries regardless of shape (``funnel_seam`` /
# ``phase_verb`` are shape-conditional — validated separately below). Used by the
# import-time validator to catch a state field accidentally named a REGISTRY_ONLY_KEY
# and thus silently stripped from the derived state model (e-5265 maint review).
_BASE_STATE_MODEL_KEYS = frozenset({
    "kind", "shape", "state_field", "advanceable_states", "routed_states",
    "ball_field", "monotonic", "phases_ref", "completion_gate", "never_terminal"})


def _validate_builtin_target_classes() -> None:
    """Fail LOUD at import if the single-source master (``BUILTIN_TARGET_CLASSES``)
    breaks an invariant an AI author could otherwise break SILENTLY (ms-142 e-5265
    AX/maint review — structure over prose reminders). Runs once at module load.

      - outer key == entry['kind']: BUILTIN_STATE_MODELS is keyed by the outer key while
        kind-dispatch resolves the ``kind`` field — a mismatch is a silent split (AX high#2).
      - aggregatable ⟺ ``arm_roles is not None``: occupation._ARM_ROLES is derived from the
        aggregatable entries; an aggregatable class with ``arm_roles=None`` would SILENTLY
        drop out of _ARM_ROLES → add_work_item / iter_evidence no-op (AX high#1). So an
        aggregatable class MUST declare a full ``arm_roles`` dict (sub-fields None when an
        arm is absent, e.g. operation's ``work_item_arm: None``); ``arm_roles=None`` is
        reserved strictly for the non-aggregatable case (acquisition) — the two ``None``
        semantics are thus disambiguated by this invariant (AX medium).
      - every derived state model carries the base keys: catches a state field accidentally
        NAMED a REGISTRY_ONLY_KEY and silently stripped (maint medium — the derive-by-strip
        collision).
      - ``funnel_seam`` present ⟺ SHAPE_FUNNEL: set_target_state's funnel branch reads it,
        so a funnel class MUST declare it (None or a seam id); a non-funnel class must not.
      - a SEAMLESS funnel (``funnel_seam is None``, e.g. account) MUST carry a non-empty
        ``phase_verb``: set_target_state interpolates it in the error path — omitting it is
        a runtime KeyError (AX medium). A SEAMED funnel (opportunity) need not."""
    for key, cls in BUILTIN_TARGET_CLASSES.items():
        assert key == cls.get("kind"), (
            f"BUILTIN_TARGET_CLASSES: outer key {key!r} != entry kind "
            f"{cls.get('kind')!r} — the outer key IS the kind, they must match")
        assert (cls.get("arm_roles") is not None) == bool(cls.get("aggregatable")), (
            f"{key}: aggregatable={cls.get('aggregatable')} must match arm_roles presence "
            "— an aggregatable class declares a full arm_roles dict; a non-aggregatable "
            "class declares arm_roles=None (else it silently drops out of _ARM_ROLES)")
        model = BUILTIN_STATE_MODELS[key]
        missing = _BASE_STATE_MODEL_KEYS - set(model)
        assert not missing, (
            f"{key}: state model missing base keys {sorted(missing)} — a state field named "
            f"one of REGISTRY_ONLY_KEYS {REGISTRY_ONLY_KEYS} would be silently stripped")
        is_funnel = cls.get("shape") == SHAPE_FUNNEL
        assert ("funnel_seam" in model) == is_funnel, (
            f"{key}: 'funnel_seam' present ⟺ SHAPE_FUNNEL (funnel classes route through it; "
            "non-funnel classes must not carry it)")
        if is_funnel and model.get("funnel_seam") is None:
            assert (model.get("phase_verb") or "").strip(), (
                f"{key}: a seamless funnel (funnel_seam=None) MUST declare a non-empty "
                "phase_verb — set_target_state interpolates it in the error path")
        # ms-155 e-5598: a declared ``deliverable`` slot must satisfy the SAME
        # {kind, projector ∈ allowlist} rule descriptor classes do — enforced via
        # the shared ``target_descriptor.validate_deliverable`` so a malformed
        # code-class deliverable fails LOUD at import rather than degrading to a
        # silent no-contribution in the root union. Lazy import (target_descriptor
        # is a pure leaf; avoids a load-order edge at module import).
        import target_descriptor as _td_v
        dl_problems = _td_v.validate_deliverable(cls.get("deliverable"), key)
        assert not dl_problems, (
            f"{key}: invalid deliverable declaration — {dl_problems}")


_validate_builtin_target_classes()


# ---------------------------------------------------------------------------
# Model resolution — built-in by kind, else derived from a data-defined
# descriptor (ms-122). "target-class が状態モデルを descriptor から引く" (T2 AC).
# ---------------------------------------------------------------------------

def _descriptor_state_model(desc: dict) -> dict:
    """Derive a state model for a descriptor-defined target-class from its
    declared phases (ms-122). Shape ``phases``: the ordered phase keys are the
    states, the descriptor's ``terminal: true`` phases are the routed (close-via)
    states, and — because ``target_engine.create_target`` seeds every descriptor
    target with a ball — it carries ``who_has_the_ball``. The non-terminal
    phases are advanceable via the descriptor engine (``set_target_state``
    delegates there)."""
    import target_descriptor as _td
    all_phases = _td.phase_keys(desc)
    terminal = set(_td.terminal_phase_keys(desc))
    advanceable = tuple(p for p in all_phases if p not in terminal)
    return {
        "kind": (desc.get("kind") or "").strip(),
        "shape": SHAPE_PHASES,
        "state_field": "phase",
        "advanceable_states": advanceable,
        "routed_states": {
            p: "beacon target close --class %s <id>" % (desc.get("kind") or "")
            for p in sorted(terminal)
        },
        # descriptors carry a ball (target_engine seeds who_has_the_ball on create);
        # the field key comes from the single source of truth in work_model.
        "ball_field": _wm.BALL_FIELD,
        "monotonic": False,
        "phases_ref": None,
        # ms-142 T3: a data-defined class had no gate; Scope B gives it the same
        # lightweight structural ban as acquisition (beacon target close refuses an
        # AI session's direct completion without a human/override signal).
        "completion_gate": GATE_SELF_CLOSE_BAN,
    }


def completion_gate_for(model: Optional[dict]) -> Optional[str]:
    """Return the completion-gate projection guarding a class's terminal
    transition (``spine`` / ``sales-judge`` / ``self-close-ban``), or ``None``
    when the model has none declared (ms-142 T3). The coverage matrix (T5) reads
    this to enforce that every target-class with a terminal state declares a gate
    — the anti-self-close capability's existence, checked from one field rather
    than re-deriving it per class."""
    if not model:
        return None
    return model.get("completion_gate")


def state_model_for(data: Optional[dict], kind: str) -> Optional[dict]:
    """Return the state model for ``kind`` — a built-in (milestone / operation /
    opportunity / acquisition), else a descriptor-defined kind's model derived
    from ``data``'s ``target_classes`` (ms-122), else ``None`` for an unknown
    kind. ``data`` may be ``None`` when only the built-ins are needed."""
    want = (kind or "").strip()
    if not want:
        return None
    builtin = BUILTIN_STATE_MODELS.get(want)
    if builtin is not None:
        return builtin
    if data is not None:
        # effective (not raw) descriptors so a profession-default target-class —
        # dev's ``release``, ms-142 e-5161 — resolves its state model here even
        # though no user declared it. Lazy import avoids the occupation↔this cycle.
        import occupation as _occ
        desc = _occ.effective_get_descriptor(data, want)
        if desc is not None:
            return _descriptor_state_model(desc)
    return None


# ---------------------------------------------------------------------------
# phase_ball derivation — the projection that DISSOLVES the ``_ARM_PHASE_BALL``
# hardwire. A class has a phase/ball pair exactly when it advances through a
# non-status field (a funnel phase or descriptor phase) AND carries a ball;
# a status-lifecycle class (milestone / operation / acquisition) has neither, so
# it derives ``None`` — byte-identical to the values the old hardcoded dict
# emitted (pinned by test_occupation_manifest).
# ---------------------------------------------------------------------------

def derive_phase_ball(model: Optional[dict]) -> Optional[dict]:
    """Return ``{"phase_field", "ball_field"}`` for a class whose state model is
    a phase funnel / phase list WITH a ball, else ``None``. This is what
    ``occupation.profession_manifest`` now emits for ``phase_ball`` instead of
    reading a standalone hardcoded map (ms-142 T2). Value invariance for the
    built-ins: milestone/operation → None (state_field ``status``), opportunity →
    ``{"phase_field": "phase", "ball_field": "who_has_the_ball"}`` (the exact keys
    of the returned dict — do not read it as ``{"phase": ...}``)."""
    if not model:
        return None
    if model.get("state_field") != "status" and model.get("ball_field"):
        return {"phase_field": model["state_field"],
                "ball_field": model["ball_field"]}
    return None


def has_generic_advance_seam(model: Optional[dict]) -> bool:
    """Whether ``set_target_state`` has a GENERIC non-terminal advance path for this
    class, or every phase/state move must go through a class verb (ms-142 e-5267).

    True for every class EXCEPT a SEAM-LESS funnel — a SHAPE_FUNNEL whose
    ``funnel_seam`` is ``None`` (account): its phase is written only by the class
    verb ``acc- phase_set`` + derivation, so ``set_target_state`` routes to the
    ``phase_verb`` error rather than run the wrong (opportunity) seam on it. Every
    other shape HAS a generic path: a SEAMED funnel (opportunity, ``funnel_seam``
    non-None) delegates to ``_advance_funnel``; a descriptor phase list delegates to
    ``_advance_descriptor``; a status-lifecycle class (milestone / operation /
    acquisition) writes its ``advanceable_states`` directly. This IS the dispatch gate
    in ``set_target_state``'s SHAPE_FUNNEL branch — that branch calls this helper, and
    the manifest's ``generic_advance`` field projects it, so the live routing decision
    and its public surface are ONE predicate defined here (e-5267 maint review: it used
    to be re-encoded in both places). It reads the model, not a kind-branch — so a
    future seam-less class shares the answer with no edit here."""
    if not model:
        return False
    if model.get("shape") == SHAPE_FUNNEL:
        return model.get("funnel_seam") is not None
    return True


def public_state_model(model: Optional[dict]) -> Optional[dict]:
    """Return the manifest-facing projection of a state model — the compact,
    uniform view every target-class entry carries (shape / state_field /
    gated_states / ball_field). The internal ``routed_states`` verb hints and
    ``advanceable_states`` stay private to this module; the manifest exposes only
    what a reader needs to reason about the class.

    ``gated_states`` (ms-142 T2 AX review) is the sorted set of states that
    ``set_target_state`` will NOT write — a class verb is required to reach them.
    It deliberately is NOT called ``terminal_states``: for a milestone the set
    includes gate-managed transitional states (``in_review`` / ``approved``) that
    are not lifecycle end-states, so "terminal" would mislead a reader into
    modelling ``in_review`` as a dead end. The name says what is true of ALL of
    them: they are gated behind a verb. Empty for a funnel whose gated phases are
    config-derived (shape ``funnel`` already signals that)."""
    if not model:
        return None
    return {
        "shape": model["shape"],
        "state_field": model["state_field"],
        "gated_states": sorted(model.get("routed_states") or {}),
        "ball_field": model.get("ball_field"),
        # ms-142 T3: which completion gate guards this class's terminal (the
        # coverage matrix checks it is non-null for every class with a terminal).
        "completion_gate": model.get("completion_gate"),
        # ms-142 e-5256: whether the class DECLARES it never settles (no 決着 grain,
        # e.g. an account's 継続 relationship). The coverage matrix's completion-gate
        # N/A predicate reads this — GENERAL, so a never-terminal class's missing gate
        # is a declared absence, not a forgotten one.
        "never_terminal": bool(model.get("never_terminal")),
        # ms-142 e-5267: whether set_target_state can advance this class on the ONE
        # generic path, or every move needs a class verb. The manifest projection of
        # the funnel_seam dispatch gate: False ONLY for a seam-less funnel (account,
        # funnel_seam=None). So a surface reader tells an opportunity (generic advance)
        # from an account (class-verb-only phase) from the declaration, not by knowing
        # the sales internals — and the raw funnel_seam id stays private to this module.
        "generic_advance": has_generic_advance_seam(model),
    }


# ---------------------------------------------------------------------------
# set_target_state — the single non-terminal advance path.
# ---------------------------------------------------------------------------

def _resolve(data: dict, target_id: str) -> tuple:
    """Return ``(record, kind)`` for ``target_id`` across every CLAIMABLE target
    collection, profession-generically. Uses ``occupation`` (the manifest-driven
    resolver) so this never indexes ``data['milestones']`` itself; falls back to a
    scan over ``occupation.claim_target_collections`` for ids the manifest resolver
    does not reach — descriptor ids AND the manifest-external claimable collection
    ``acquisitions`` (ms-142 e-5256: accounts MOVED INTO the manifest, so it IS in
    ``iter_target_records`` now; only acquisitions still rides a separate persistence
    path). Without that widening an acquisition (whose state model IS declared) could
    not be resolved here (ms-142 T2 maintainability review). Lazy import avoids an
    import cycle (occupation eager-imports this module)."""
    import occupation as _occ
    rec = _occ.find_target(data, target_id)
    if rec is None:
        for coll in _occ.claim_target_collections(data):
            rec = next((r for r in (data.get(coll) or [])
                        if isinstance(r, dict) and r.get("id") == target_id), None)
            if rec is not None:
                break
    if rec is None:
        raise TargetStateError(
            f"target not found: {target_id}. Ids are prefixed by class "
            f"(ms- milestone / op- operation / opp- opportunity / acc- account / "
            f"acq- acquisition, plus any descriptor prefix). "
            f"List targets with `beacon status`.")
    kind = _wm.target_kind(target_id) or (rec.get("kind") or "")
    return rec, kind


def set_target_state(data: dict, target_id: str, to_state: str, *,
                     actor: str = "", reason: str = "") -> tuple:
    """Advance a Target to a NON-TERMINAL ``to_state`` on the one generic path,
    whatever its class. Returns ``(record, old_state, new_state)``.

    The single contract across all target-classes (ms-142 T2):

      * A TERMINAL or gate-managed ``to_state`` (milestone done/observing/
        cancelled/in_review/approved, operation closed, acquisition done/
        cancelled, a descriptor terminal phase) is REFUSED — the error names the
        class verb to route through. This is the structural completion-gate
        non-bypass: set_target_state can never land a milestone on done/observing
        behind the ms-119 review gate.
      * A monotonic class (operation / acquisition) validates the move against
        ``core.LIFECYCLE_TRANSITIONS`` (illegal jumps like ``open → todo`` raise).
      * A permissive class (milestone status enum) writes any advanceable state.
      * A descriptor class delegates to the proven ``target_engine.advance_target``
        (its phases, required-field checks, and phase_history are reused).
      * The opportunity FUNNEL delegates the non-terminal write to the sales seam
        ``sales_entities.advance_funnel_phase`` (phase + mirrored status, ms-142
        e-5169); a terminal (決着) phase is refused as the sales judge gate's
        completion claim.

    The write is generic (the state field + a ``meta['{state}_at/by/reason']``
    stamp matching each status class's own audit convention); it is additive —
    the existing class verbs are unchanged, so no production flow's behaviour
    moves. Wiring those verbs onto this path is the follow-up."""
    want = (to_state or "").strip()
    if not want:
        raise TargetStateError("to_state is required")
    rec, kind = _resolve(data, target_id)
    model = state_model_for(data, kind)
    if model is None:
        raise TargetStateError(
            f"no state model for target-class {kind!r} (id {target_id})")

    shape = model["shape"]

    # Descriptor phases: delegate to the descriptor engine (already the generic
    # advance path for data-defined classes). It enforces its own terminal /
    # required-field rules, so terminal refusal there is the engine's job; we
    # keep the terminal guard here for the built-ins.
    if shape == SHAPE_PHASES:
        return _advance_descriptor(data, kind, target_id, want,
                                   actor=actor, reason=reason)

    # Opportunity funnel: delegate to the sales seam via a dedicated helper — the
    # SHAPE_FUNNEL twin of the SHAPE_PHASES → _advance_descriptor delegation, so
    # the two config-derived classes route their non-terminal write through the
    # same thin shape (ms-142 e-5169). Keeping the sales-specific terminal check
    # inside _advance_funnel is what stops the gate logic from spreading across
    # set_target_state.
    if shape == SHAPE_FUNNEL:
        if not has_generic_advance_seam(model):
            # ms-142 e-5256/e-5267: a funnel with NO generic seam (Account) has its phase
            # written by a class verb (``acc- phase_set``, a direct write) + derivation,
            # not the opportunity status-mirror writer ``_advance_funnel``. Route to the
            # class's own ``phase_verb`` hint rather than run the wrong seam on it.
            # ONE predicate: ``has_generic_advance_seam`` IS this dispatch gate, and the
            # manifest's ``generic_advance`` projection reads the SAME helper — so the gate
            # and its public surface cannot drift (e-5267 maint review consensus: the
            # funnel_seam pivot was encoded here AND in the helper; now defined once).
            # Declarative (reads the model), not a kind-branch, and the verb string is
            # model-declared so no profession-specific text is hardcoded here.
            verb_hint = (model.get("phase_verb") or "").format(target_id=target_id)
            raise TargetStateError(
                f"{target_id}: {kind} phase は set_target_state の generic funnel "
                f"seam を持ちません。"
                + (verb_hint or "クラス固有の phase 変更 verb で進めてください。"))
        return _advance_funnel(data, target_id, rec, model["state_field"], want)

    state_field = model["state_field"]
    old = rec.get(state_field, "")

    # Terminal / gate-managed refusal — the completion-gate non-bypass.
    routed = model.get("routed_states") or {}
    if want in routed:
        raise TargetStateError(
            f"{target_id}: {want!r} is a terminal/gated state — "
            f"set_target_state writes only non-terminal transitions. "
            f"Route it through: {routed[want]}")

    advanceable = model.get("advanceable_states") or ()
    if want not in advanceable:
        # AX review: name the concrete gated states + their verbs inline, rather
        # than pointing at the internal ``routed_states`` field a caller can't see.
        gated = "; ".join(f"{s} → {v}" for s, v in sorted(routed.items())) or "(none)"
        raise TargetStateError(
            f"unknown non-terminal {kind} state {want!r}. "
            f"Advanceable via set_target_state: {sorted(advanceable)}. "
            f"Gated states (use the named verb): {gated}")

    if model.get("monotonic"):
        import core
        try:
            core.validate_lifecycle_transition(kind, old, want)
        except ValueError as exc:
            raise TargetStateError(str(exc)) from exc

    # Generic write + audit stamp (matches the status classes' meta convention).
    rec[state_field] = want
    meta = rec.setdefault("meta", {})
    stamp = work_base.now_iso()
    meta[f"{want}_at"] = stamp
    meta[f"{want}_by"] = actor or work_base.current_actor()
    if reason:
        meta[f"{want}_reason"] = reason
    return rec, old, want


def _advance_descriptor(data: dict, kind: str, target_id: str, to_phase: str, *,
                        actor: str, reason: str) -> tuple:
    """Delegate a descriptor-defined class's phase advance to
    ``target_engine.advance_target``. Refuses a terminal phase here (route via
    ``beacon target close``) so the descriptor path honours the same
    non-terminal-only contract the built-ins do."""
    import target_engine as _te
    import occupation as _occ
    # effective descriptors so a profession-default class (release) advances too.
    desc = _occ.effective_get_descriptor(data, kind)
    if desc is None:
        raise TargetStateError(f"no descriptor for target-class {kind!r}")
    if _te.is_terminal_phase(desc, to_phase):
        raise TargetStateError(
            f"{target_id}: phase {to_phase!r} is terminal — "
            f"route through `beacon target close --class {kind} {target_id}`")
    return _te.advance_target(data, desc, target_id, to_phase=to_phase,
                              actor=actor, reason=reason)


def _advance_funnel(data: dict, target_id: str, rec: dict, state_field: str,
                    to_phase: str) -> tuple:
    """Delegate an opportunity funnel's non-terminal advance to the sales seam
    ``sales_entities.advance_funnel_phase`` (the SHAPE_FUNNEL twin of
    ``_advance_descriptor``). Refuses a terminal (決着) phase here — a settlement
    is a completion claim owned by the sales judge gate (GATE_SALES_JUDGE), which
    set_target_state must not bypass. Which phases are terminal is CONFIG-derived
    (per project), so the check lives in the sales adapter and stays inside this
    one helper rather than spreading across set_target_state. The seam writes phase
    + mirrored status; the account rollup + ``at`` stay with the caller
    (``phase_set``), so status 同期 is invariant. Returns ``(rec, old, new)``."""
    import sales_entities as _se
    old = rec.get(state_field, "")
    if _se.opportunity_phase_is_terminal(data, to_phase):
        raise TargetStateError(
            f"{target_id}: {to_phase!r} is a terminal (決着) phase — "
            f"set_target_state writes only non-terminal transitions. Settle the "
            f"deal via `beacon opportunity phase {target_id} {to_phase}` (the "
            f"funnel's completion gate), not the generic advance path.")
    _se.advance_funnel_phase(data, rec, to_phase)
    return rec, old, to_phase
