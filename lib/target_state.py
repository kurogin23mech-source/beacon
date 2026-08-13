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
#                       (GATE_SPINE / GATE_SALES_JUDGE / GATE_SELF_CLOSE_BAN);
#                       the anti-self-close capability's declared existence (T3).
#
# The advanceable/routed split is the SINGLE source for "which states does a
# class own and which need a class verb". ``core.VALID_STATUSES`` /
# ``VALID_OPERATION_STATUSES`` / ``ACQUISITION_STATUSES`` and
# ``LIFECYCLE_TRANSITIONS`` remain the canonical VOCABULARY + monotonic guard;
# this model references them (monotonic=True → validate via the table) rather
# than duplicating the transitions, so the two cannot drift.
# ---------------------------------------------------------------------------

BUILTIN_STATE_MODELS: dict[str, dict] = {
    "milestone": {
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
    },
    "operation": {
        "kind": "operation",
        "shape": SHAPE_TRANSITION_TABLE,
        "state_field": "status",
        "advanceable_states": ("todo", "in_progress", "open"),
        "routed_states": {"closed": "beacon operation close <id>"},
        "ball_field": None,
        "monotonic": True,   # validated via core.LIFECYCLE_TRANSITIONS['operation']
        "phases_ref": None,
        "completion_gate": GATE_SPINE,   # same dev spine as milestone (close)
    },
    "acquisition": {
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
    },
    "opportunity": {
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
        # ms-142 e-5256: this funnel HAS a generic set_target_state seam — a
        # non-terminal advance delegates to ``_advance_funnel`` (the sales seam that
        # writes phase + mirrored status). ``funnel_seam`` names it so a SIBLING
        # funnel WITHOUT one (account) is a declared distinction, not a kind-branch.
        "funnel_seam": "sales-opportunity",
    },
    "account": {
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
        # set_target_state to a clear "use the class verb" error rather than the wrong
        # (opportunity) writer — declaratively (reads the model), not a kind-branch.
        "funnel_seam": None,
    },
}


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
    }


# ---------------------------------------------------------------------------
# set_target_state — the single non-terminal advance path.
# ---------------------------------------------------------------------------

def _resolve(data: dict, target_id: str) -> tuple:
    """Return ``(record, kind)`` for ``target_id`` across every CLAIMABLE target
    collection, profession-generically. Uses ``occupation`` (the manifest-driven
    resolver) so this never indexes ``data['milestones']`` itself; falls back to a
    scan over ``occupation.claim_target_collections`` for ids the manifest resolver
    does not reach — descriptor ids AND the sales secondary collections
    (accounts / acquisitions) that ride a separate persistence path and so are NOT
    in ``iter_target_records``. Without that widening an acquisition (whose state
    model IS declared) could not be resolved here (ms-142 T2 maintainability
    review). Lazy import avoids an import cycle (occupation eager-imports this
    module)."""
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
            f"(ms- milestone / op- operation / opp- opportunity / acq- acquisition, "
            f"plus any descriptor prefix). List targets with `beacon status`.")
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
        if model.get("funnel_seam") is None:
            # ms-142 e-5256: a ball-less, never-terminal funnel (Account) has no
            # generic set_target_state seam — its phase is written by the class verb
            # (``acc- phase_set``, a direct write) + derivation, not the opportunity
            # status-mirror writer ``_advance_funnel``. Route to the verb rather than
            # run the wrong seam on it. Declarative (reads the model), not a
            # kind-branch.
            raise TargetStateError(
                f"{target_id}: {kind} phase は set_target_state の generic funnel "
                f"seam を持ちません (継続関係で status mirror が無い)。"
                f"`beacon account phase {target_id} <phase>` で進めてください "
                f"(顧客フェーズは商談の成約からも自動 derive されます)。")
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
