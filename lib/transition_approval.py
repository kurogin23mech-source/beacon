"""Profession-neutral target-transition approval (目的達成レビュー primitive).

A target (milestone / opportunity / operation …) advancing to a *goal-attainment*
state carries a claim: "this target reached / earned the next step of its goal."
That verdict is *owned* by the human (the target owner), not *discoverable* by the
AI against a written 原典 — so the AI assembles intent + evidence and the human
approves. This is the profession-neutral generalization of what sales already
does today.

Design razor — avoid over-gating and double-gating:

- **Gate** only transitions that assert goal-attainment:
    - dev milestone  → done / closed / observing   (completion claim)
      NOTE on observing: in this codebase `observing` is NOT a soft/reversible
      "monitoring" pause — it is the 運用改善フェーズ that *presupposes the
      milestone's basic goal is already reached* (you only put a milestone into
      operation once it works). So moving to observing carries the same
      "目的を果たした" claim as done: if a 目的達成 / 思想 deviation existed you
      would NOT put it into operation (= would not observe it). It is therefore
      an attainment transition and is gated like done — BUT only when it comes
      from a state where work actually happened. todo -> observing attains
      nothing, and done/closed/cancelled -> observing is a re-open, so those are
      routine (not gated). See _OBSERVING_NON_ATTAINMENT_FROM.
    - ops operation  → closed               (retirement claim)
    - sales opportunity → forward funnel advance, or terminal (each funnel step
      is "we earned the right to advance" — reviewed against the meeting
      evidence)
- **Do NOT gate** transitions that merely *begin* or *pause* work:
    - todo → active / in_progress (start), → waiting (pause). These carry no
      attainment claim; the AI / user just does them.

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
    # observing is a completion claim in this codebase (運用改善フェーズ =
    # 基本目的達成が前提), not a reversible pause — see module docstring.
    "milestone": frozenset({"done", "closed", "observing"}),
    "operation": frozenset({"closed"}),
}

# For a milestone, -> observing is a completion claim only when it comes FROM a
# state where work actually happened. The claim (運用改善フェーズ = 基本目的達成が
# 前提) presupposes prior work, so:
#   - todo -> observing attains nothing (never started) → routine, not a claim.
#   - done/closed/cancelled -> observing is a re-open (already terminal), not a
#     fresh completion claim.
# Everything else (in_progress / active / waiting / in_review) implies prior work.
_OBSERVING_NON_ATTAINMENT_FROM = frozenset({"todo", "done", "closed", "cancelled"})

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
    completion (dev milestone → done / closed / observing; ops → closed) and
    funnel-advance / terminal (sales). False for entry (todo -> active) and
    pause (-> waiting). (observing is a completion claim here, not a pause —
    see the module docstring.)

    `funnel` (optional): ordered list of the opportunity's phase names, so a
    "forward" move can be distinguished from a corrective backward jump.
    """
    if new_state in _COMPLETION_STATES.get(target_kind, ()):
        # -> observing is a completion claim only from a work state (see
        # _OBSERVING_NON_ATTAINMENT_FROM): todo -> observing attains nothing,
        # done -> observing is a re-open. done/closed are unconditional claims.
        if target_kind == "milestone" and new_state == "observing":
            return old_state not in _OBSERVING_NON_ATTAINMENT_FROM
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
            # ms-119 / e-4205: slot for INDEPENDENT review evidence (the 目的達成
            # judge's verdict + grounds), distinct from `intent` (the implementer's
            # completion CLAIM) and `evidence` (implementer-supplied refs). Empty at
            # creation; the approve path refuses a SILENT approval while it is empty.
            "review_evidence": [],
            "approval_status": "pending",
            "approval_rationale": None,
            "approval_history": [],
            "actor": actor,
        },
    }


def append_review_evidence(entry, *, verdict, summary, source, actor, at):
    """Record an INDEPENDENT review's evidence onto a pending transition-approval
    (ms-119 / e-4205).

    Distinct from ``meta["intent"]`` (the implementer's completion CLAIM) and
    ``meta["evidence"]`` (implementer-supplied refs): this slot holds the verdict +
    grounds an INDEPENDENT judge produced against the target's SPEC (the 目的達成
    review). e-4005 required the evidence not be the implementer's self-report, but
    nothing tied "an independent judge actually ran" to the approval — so a
    completion could be approved on the implementer's word alone (observed with
    e-4198). This slot + the approve guard (``has_independent_evidence``) close the
    SILENT path: the human still owns the verdict, but approving with no independent
    evidence now requires an explicit acknowledgement, never silence.

    Append-only (audit): each record is ``{verdict, summary, source, actor, at}``.
    Pure — the caller owns persistence.
    """
    rec = {
        "verdict": verdict,
        "summary": summary,
        "source": source,
        "actor": actor,
        "at": at,
    }
    entry.setdefault("meta", {}).setdefault("review_evidence", []).append(rec)
    return entry


def has_independent_evidence(entry):
    """True if the approval carries ≥1 independent review-evidence record (ms-119 /
    e-4205). The approve path uses this so a completion cannot be approved on the
    implementer's intent alone unless the human EXPLICITLY acknowledges the absence
    — a conscious choice, never silent."""
    return bool((entry.get("meta") or {}).get("review_evidence"))


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


def append_verdict(entry, *, status, rationale="", actor="", at, gate=None):
    """Record a human verdict on a transition-approval entry.

    Mirrors the PR review-history pattern (append-only audit + status flip).
    Returns the `new_state` to apply if approved, else None (so the caller knows
    whether to execute the pending transition).

    ms-119 / e-4006 audit (思想レビュー finding ①b): ``gate`` records HOW the
    approval passed the human-only guard — which signal opened it
    (``BEACON_SESSION_KIND=human`` vs ``BEACON_TARGET_APPROVE_USER_OVERRIDE=1``),
    the raw session kind, and any declared evidence provenance. The env guard is
    a self-report, so the value of this MS is not "AI can't approve" but "an AI
    that approves cannot HIDE it" — an AI-session override approval leaves a
    grep-able footprint in the record instead of looking identical to a human's.
    """
    if status not in ("approved", "rejected"):
        raise ValueError("invalid verdict status: %r" % (status,))
    m = entry["meta"]
    m["approval_status"] = status
    m["approval_rationale"] = rationale
    hist = {
        "status": status,
        "rationale": rationale,
        "actor": actor,
        "at": at,
    }
    if gate:
        hist["gate"] = gate
        m["approval_gate"] = gate
    m["approval_history"].append(hist)
    entry["status"] = "approved" if status == "approved" else "cancelled"
    entry["done_at"] = at
    return m["new_state"] if status == "approved" else None
