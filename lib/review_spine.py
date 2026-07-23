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
REVIEW_AX = "ax"                   # AX: AI-Experience interface drift (advisory)

# The independence contract handed to the judge subagent. It is the *structural*
# form of AX 原典 §2 (計器の必然): the judge is a context-zero instrument, so the
# bundle below must be its COMPLETE input. If the judge reaches for the
# implementer's session narrative / commit intent, the instrument is tainted and
# the review is void (ms-119 e-3947).
INDEPENDENCE_CONTRACT = (
    "この bundle が判定の完全な入力です。実装者セッションの会話文脈・コミットの"
    "意図説明・『本当はこうするつもり』を一切参照しないでください (AX 原典 §2 "
    "計器の必然)。原典 (origin) と機械採取した差分 (artifact) だけから判定する。"
    "文脈ゼロでない judge は計器として無効です。"
)


def assemble_review_context(review_type, *, origin_id, origin_content,
                            diff_text, mode, target_ref, gaps=None):
    """Assemble the review-kernel bundle handed to an independent judge subagent.

    This is the *structural* enforcement of reviewer independence (ms-119
    e-3947): the bundle carries ONLY the 原典 (origin — a written source such as
    the AX principles or a target's SPEC) and the mechanically-collected diff
    (artifact). It deliberately carries no implementer narrative, so a judge fed
    this bundle cannot self-review even if the same human drives both sessions.

    Pure — callers collect origin_content (file / doc read) and diff_text (git)
    mechanically and pass them in; this function only shapes the bundle so the
    shaping is unit-testable without a repo or a subagent.

    Args:
        review_type: REVIEW_AX / REVIEW_PHILOSOPHY (attainment is human-gated,
            not a subagent judge — see review_bindings_for_transition).
        origin_id: identifier of the 原典 (doc-id or repo-relative file path).
        origin_content: the full text of the 原典 (never a summary).
        diff_text: the mechanically-collected unified diff (git / gh pr diff).
        mode: "diff" (change-scoped) or "full-surface" (snapshot audit).
        target_ref: what was reviewed (PR number / ref range / target id).
        gaps: optional list of gentle gaps to surface (e.g. philosophy review of
            a SPEC-less target — the missing 原典 is itself a finding, 方針5).
    """
    if review_type not in (REVIEW_AX, REVIEW_PHILOSOPHY):
        raise ValueError(
            f"assemble_review_context: unsupported review_type {review_type!r} "
            f"(judge-run reviews are {REVIEW_AX!r} / {REVIEW_PHILOSOPHY!r}; "
            f"{REVIEW_ATTAINMENT!r} is human-gated, not a subagent judge)."
        )
    if mode not in ("diff", "full-surface"):
        raise ValueError(f"assemble_review_context: mode must be 'diff' or "
                         f"'full-surface', got {mode!r}.")
    return {
        "review_type": review_type,
        "mode": mode,
        "target_ref": target_ref,
        "origin": {"id": origin_id, "content": origin_content},
        "artifact": {"kind": "diff", "ref": target_ref, "content": diff_text},
        "independence_contract": INDEPENDENCE_CONTRACT,
        "gaps": list(gaps or []),
    }


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
