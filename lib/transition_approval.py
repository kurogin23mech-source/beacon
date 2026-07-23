"""Profession-neutral target-transition approval (目的達成レビュー primitive).

A target (milestone / opportunity / operation …) advancing to a *goal-attainment*
state carries a claim: "this target reached / earned the next step of its goal."
That verdict is *owned* by the human (the target owner), not *discoverable* by the
AI against a written 原典 — so the AI assembles intent + evidence and the human
approves. This is the profession-neutral generalization of what sales already
does today.

Design razor — avoid over-gating and double-gating:

- **Gate** only transitions that assert goal-attainment:
    - dev milestone  → done / closed        (completion claim)
    - ops operation  → closed               (retirement claim)
    - sales opportunity → forward funnel advance, or terminal (each funnel step
      is "we earned the right to advance" — reviewed against the meeting
      evidence)
- **Do NOT gate** transitions that merely *begin* or *pause* work, or move to a
  soft / reversible monitoring state:
    - todo → active / in_progress (start), → waiting (pause), → observing
      (monitoring). These carry no attainment claim; the AI / user just does
      them.

Projections (who enforces the gate today):

- **sales** already has a *working projection* of this review:
  `/beacon-sales-meeting-wrap` reviews the meeting transcript and proposes
  advance / retry / terminal; `opportunity judge` records the human verdict.
  The spine must NOT stack a second gate on top — it subsumes that flow in a
  later task. So the NEW primitive enforces only the professions that currently
  LACK a gate: **dev (milestone) + ops (operation)**.

`is_attainment_transition()` is the profession-neutral *truth* (used for
review-worthiness, includes sales advances). `requires_spine_approval()` is the
narrower question of which transitions the NEW primitive enforces *right now*
(dev + ops only), so that we neither over-gate routine transitions nor
double-gate sales.
"""

# Terminal-completion states per target kind that carry a goal-attainment claim.
_COMPLETION_STATES = {
    "milestone": frozenset({"done", "closed"}),
    "operation": frozenset({"closed"}),
}

# Opportunity terminal-verdict states (funnel-independent).
_OPPORTUNITY_TERMINAL = frozenset({"terminal", "closed_won", "closed_lost", "lost", "won"})

# Professions whose attainment transitions the NEW spine primitive enforces
# today. Sales is intentionally absent: its meeting-wrap → judge flow is the
# existing projection of this review, and stacking a second gate would
# over-gate the working sales path.
_SPINE_ENFORCED_KINDS = frozenset({"milestone", "operation"})


def is_attainment_transition(target_kind, old_state, new_state, *, funnel=None):
    """Does this transition assert the target reached / advanced toward its goal?

    Profession-neutral truth, independent of who enforces the gate. True for
    completion (dev / ops) and funnel-advance / terminal (sales). False for
    entry (todo -> active), pause (-> waiting), and soft monitoring
    (-> observing).

    `funnel` (optional): ordered list of the opportunity's phase names, so a
    "forward" move can be distinguished from a corrective backward jump.
    """
    if new_state in _COMPLETION_STATES.get(target_kind, ()):
        return True
    if target_kind == "opportunity":
        if new_state in _OPPORTUNITY_TERMINAL:
            return True
        if funnel and old_state in funnel and new_state in funnel:
            return funnel.index(new_state) > funnel.index(old_state)
    return False


def requires_spine_approval(target_kind, old_state, new_state, *, funnel=None):
    """Should the NEW transition-approval primitive gate this transition?

    True only when both hold:
      (a) the transition is a goal-attainment claim, AND
      (b) this profession has no existing gate projection (dev / ops only).

    This prevents over-gating (routine transitions like todo -> active) and
    double-gating (sales, whose judge flow already reviews + confirms).
    """
    if target_kind not in _SPINE_ENFORCED_KINDS:
        return False
    return is_attainment_transition(target_kind, old_state, new_state, funnel=funnel)


def build_transition_approval(*, entry_id, target_id, target_kind, old_state,
                              new_state, intent, evidence=None, actor="",
                              created_at):
    """Build a pending transition-approval entry (profession-neutral).

    approve = the transition executes; reject = it does not. The AI assembles
    `intent` (why it believes the attainment claim is met) + `evidence` (refs:
    commits / communications / docs); the human owns the verdict.

    Pure: returns the entry dict. The caller (CLI / core) owns entry-id
    allocation and persistence.
    """
    return {
        "id": entry_id,
        "type": "target-transition-approval",
        "description": "%s: %s -> %s approval" % (target_id, old_state, new_state),
        "created_at": created_at,
        "done_at": None,
        "status": "pending",
        "meta": {
            "target_id": target_id,
            "target_kind": target_kind,
            "old_state": old_state,
            "new_state": new_state,
            "intent": intent,
            "evidence": list(evidence or []),
            "approval_status": "pending",
            "approval_rationale": None,
            "approval_history": [],
            "actor": actor,
        },
    }


def assess_completion_criteria(*, has_spec, objective="", acceptance="", intent=""):
    """Surface gaps in a target's completion criteria (ms-119 / e-3911 §5 AC6).

    The 目的達成 review works best when the target carries written, reviewable
    success criteria (a SPEC 原典, an objective, or acceptance criteria). When
    it doesn't, the review-request must NOT hard-block — it should still be
    created — but it should *gently surface the gap* so the human approver
    knows there's nothing solid to check the completion claim against, and that
    the AI is inferring intent rather than reading a written condition.

    Returns a list of gap reason codes (empty = the target has enough written
    criteria to review against):
      - ``"no_written_criteria"``: no SPEC, no objective, no acceptance — the
        completion claim can't be checked against anything written.
      - ``"no_intent"``: the requester supplied no intent, so even the inferred
        "why is this done" is missing.

    Pure: takes primitive fields, not the target dict, so it is trivial to test
    and decoupled from target-kind-specific shapes.
    """
    reasons = []
    if not has_spec and not (objective or "").strip() and not (acceptance or "").strip():
        reasons.append("no_written_criteria")
    if not (intent or "").strip():
        reasons.append("no_intent")
    return reasons


_CRITERIA_GAP_TEXT = {
    "no_written_criteria": (
        "完了条件 (SPEC / 目的 / 受入条件) が原典に一つも記載されていません。"
        "何を満たせば done かが書かれていないので、承認前に基準を確認してください"
    ),
    "no_intent": (
        "達成理由 (intent) が渡されていません。「なぜ done と考えるか」を明示すると "
        "レビュアーが判断しやすくなります"
    ),
}


def format_criteria_gap(reasons, *, target_id):
    """Render a non-blocking gap warning for a target's weak completion criteria.

    Returns "" when there are no gaps (契約は format_pending_dm_summary と同じ:
    空なら呼び出し側がセクションごと省略する)."""
    if not reasons:
        return ""
    lines = [f"⚠ 完了条件の gap ({target_id}) — hard-block はしません、依頼は作成済:"]
    for r in reasons:
        text = _CRITERIA_GAP_TEXT.get(r)
        if text:
            lines.append(f"  - {text}")
    return "\n".join(lines)


def append_verdict(entry, *, status, rationale="", actor="", at):
    """Record a human verdict on a transition-approval entry.

    Mirrors the PR review-history pattern (append-only audit + status flip).
    Returns the `new_state` to apply if approved, else None (so the caller knows
    whether to execute the pending transition).
    """
    if status not in ("approved", "rejected"):
        raise ValueError("invalid verdict status: %r" % (status,))
    m = entry["meta"]
    m["approval_status"] = status
    m["approval_rationale"] = rationale
    m["approval_history"].append({
        "status": status,
        "rationale": rationale,
        "actor": actor,
        "at": at,
    })
    entry["status"] = "approved" if status == "approved" else "cancelled"
    entry["done_at"] = at
    return m["new_state"] if status == "approved" else None
