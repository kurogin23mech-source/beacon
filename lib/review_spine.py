"""Review firing spine — which reviews a target lifecycle transition binds
(ms-119 / e-3911).

The review-kernel (ms-119) fires reviews at 節目 (= the moments where the cost
of a wrong call jumps: a lifecycle transition, a completion claim). Beacon owns
the target lifecycle, so it can emit a trigger GitHub never could — a target's
phase transition / close. This module is the *pure* binding table: given a
transition, which review(s) apply. The trigger writer + the wiring into the
concrete transition commands live in commands.py.

Two of the four reviews bind to a target transition:

- **目的達成 (attainment)**: owned verdict — the human confirms the target met
  its goal. Its *blocking* mechanism is the approval entry from e-3912
  (`beacon target review-request` → human approve). So the spine does NOT
  re-fire it when the transition is already going through that gate
  (``gated=True``). It fires only as an *advisory nudge* when a completion
  transition was applied WITHOUT the gate — a forcing function toward the
  reviewed path, never a second blocking mechanism.
- **思想 (philosophy)**: discoverable drift vs the written 原典 (SPEC / vision).
  Fires on a completion claim of a SPEC-bearing target, advisory (did the
  implementation drift from what the SPEC promised?).

**AX is intentionally absent here**: it binds to interface-change (diff / PR)
events, not to a target lifecycle transition. A milestone going done is not an
interface change.
"""

import transition_approval as _ta

# Review-type identifiers (stable strings — trigger payloads persist them).
REVIEW_ATTAINMENT = "attainment"   # 目的達成: owned verdict, human approval
REVIEW_PHILOSOPHY = "philosophy"   # 思想: drift vs SPEC / vision (advisory)


def review_bindings_for_transition(target_kind, old_state, new_state, *,
                                   has_spec=False, gated=False):
    """Return the review bindings that apply to a target lifecycle transition.

    A binding is a dict: ``{"review": <id>, "blocking": bool, "origin": str}``.
    ``origin`` names the 原典 (the written source the judge checks against) so
    the downstream message can explain what the review compares against.

    Empty list = no review fires (routine / reversible transitions like
    todo -> active, -> waiting, -> observing carry no completion claim).

    Args:
        target_kind: "milestone" / "operation" / "opportunity" (prefix-derived).
        old_state / new_state: the transition's endpoints.
        has_spec: whether the target has a SPEC / vision 原典 attached.
        gated: True when this transition is already going through the 目的達成
            approval gate (``beacon target ... --review``). Suppresses the
            attainment nudge so the spine never double-surfaces it.
    """
    if not _ta.is_attainment_transition(target_kind, old_state, new_state):
        return []
    bindings = []
    if not gated:
        # Completion applied without the review gate — advisory nudge toward it.
        bindings.append({
            "review": REVIEW_ATTAINMENT,
            "blocking": False,
            "origin": "owner intent (target が目的を果たしたか)",
        })
    if has_spec:
        bindings.append({
            "review": REVIEW_PHILOSOPHY,
            "blocking": False,
            "origin": "SPEC / vision",
        })
    return bindings
