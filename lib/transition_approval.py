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


# ms-119 / e-4205: the closed value domain for a review-evidence verdict. It is the
# single source of the vocabulary — the CLI usage / error / guidance strings render
# from it and append_review_evidence REJECTS anything else, so an unvalidated verdict
# (`--verdict ok`) can neither enter the append-only audit ledger nor unlock the
# approve gate (#504 AX+maint review: an unchecked enum was silent corruption).
REVIEW_EVIDENCE_VERDICTS = ("attained", "partial", "not-attained")


def append_review_evidence(entry, *, verdict, summary, source, actor, at):
    """Record a review's evidence onto a pending transition-approval (ms-119 /
    e-4205).

    Distinct from ``meta["intent"]`` (the implementer's completion CLAIM) and
    ``meta["evidence"]`` (implementer-supplied refs): this slot holds a verdict +
    grounds produced by a 目的達成 review judge against the target's SPEC. e-4005
    required the evidence not be the implementer's self-report, but nothing tied "a
    review judge actually ran" to the approval — so a completion could be approved on
    the implementer's word alone (observed with e-4198). This slot + the approve
    guard (``has_review_evidence``) close the SILENT path: approving with no evidence
    now needs an explicit acknowledgement, never silence.

    ``verdict`` is validated against ``REVIEW_EVIDENCE_VERDICTS`` — an invalid value
    raises ValueError rather than entering the append-only ledger. ``source`` is the
    self-declared provenance (recorded, NOT structurally verified to be independent —
    see ``has_review_evidence``); the caller passes it verbatim (no fabricated
    default).

    Append-only (audit): each record is ``{verdict, summary, source, actor, at}``.
    Pure — the caller owns persistence.
    """
    if verdict not in REVIEW_EVIDENCE_VERDICTS:
        raise ValueError(
            "invalid review-evidence verdict %r (expected one of %s)"
            % (verdict, ", ".join(REVIEW_EVIDENCE_VERDICTS)))
    rec = {
        "verdict": verdict,
        "summary": summary,
        "source": source,
        "actor": actor,
        "at": at,
    }
    entry.setdefault("meta", {}).setdefault("review_evidence", []).append(rec)
    return entry


def has_review_evidence(entry):
    """True if the approval carries ≥1 review-evidence record (ms-119 / e-4205).

    NAME IS DELIBERATELY MODEST (#504 AX+maint review): this checks only that a
    record EXISTS, not that it is genuinely INDEPENDENT. Independence is an
    OPERATIONAL property of how the record got attached (a Skill running a
    context-zero judge, then `beacon target attach-review-evidence`), NOT a
    structural guarantee — an implementer could attach their own judgement and it
    would pass, exactly like the env-based approve guard is a self-report. What is
    structural is: the absence of a record cannot pass SILENTLY (the approve gate
    refuses), and the record's provenance (source + actor) is on the ledger, so a
    self-attached one is grep-able rather than hidden. Do not rename this back to
    imply a guarantee the structure does not make."""
    return bool((entry.get("meta") or {}).get("review_evidence"))


# ms-119 / e-4579: the priority tiers whose UNSTARTED tasks are a strong "goal not
# yet met" prior. A task at highest/high is a *known, encoded* piece of work the
# owner already judged important; leaving it untouched while claiming attainment is
# the "掃除機がある≠掃除した" hole (a mechanism exists in code ≠ the goal was reached).
# medium/low/lowest are NOT gated — attainment is defined on OUTCOMES (AC), not on
# draining every task, and lower-tier backlog is normal residue, not a completion
# blocker. (See wave8 2026-07-29: attainment read 11/12 while 1 highest + 3 high
# tasks sat untouched.)
BACKLOG_GATED_PRIORITIES = ("highest", "high")

# ms-119 / e-4579: the closed value domain for a backlog-task disposition. Each
# unstarted highest/high task must carry EXACTLY one of these before the target's
# attainment can be approved — so "important but skippable" is never a SILENT miss:
#   - "done"             : the work was in fact completed (status flip pending, or
#                          the task is being closed as part of this attainment).
#   - "superseded"       : the task is no longer needed for the goal (a hypothesis
#                          that turned out unnecessary). REASON REQUIRED — the
#                          judge / human must say WHY it can be dropped.
#   - "blocks-attainment": the task is still required and NOT done, so it actively
#                          blocks the attainment claim. #551 MUST-2: this is now
#                          ENFORCED, not merely recorded — approve refuses while any
#                          gated task carries this disposition (see
#                          blocks_attainment_backlog). "attained にできない" is
#                          structural, not advisory.
DISPOSITION_VERDICTS = ("done", "superseded", "blocks-attainment")


# The status vocabulary, bucketed. Each canonical live status is listed EXACTLY once
# so the coarse buckets the backlog gate reads are an explicit enumeration, not a
# fail-open catch-all (#551 MEDIUM maint review). An UNKNOWN status is deliberately
# NOT mapped to "in_progress": that was fail-OPEN (an unrecognised status silently
# escaped the gate). A gate that protects a completion claim must fail CLOSED — an
# unknown status is treated as "unstarted" (gated), so a typo / new vocabulary word
# surfaces as a disposition demand rather than a silent skip.
_STARTED_STATUSES = frozenset({
    "in_progress", "in_review", "waiting", "working", "leader_review",
    "user_review", "active", "blocked",
})
_DONE_STATUSES = frozenset({"done", "closed"})
_CANCELLED_STATUSES = frozenset({"cancelled", "canceled"})
_UNSTARTED_STATUSES = frozenset({"", "todo"})


def normalize_task_status(status):
    """Canonicalize a task status to the coarse buckets the backlog gate cares about.

    Returns "unstarted" for a task that never began (todo / "" / **or any unknown
    status** — fail-closed), "done" for a completed one, "cancelled" for a
    soft-deleted one, and "in_progress" for anything in flight (in_progress /
    in_review / waiting / working / leader_review / user_review / active …).

    Only "unstarted" highest/high tasks are gated. The buckets are an EXPLICIT
    enumeration (``_STARTED_STATUSES`` etc.), NOT a catch-all — the previous
    ``return "in_progress"`` fallthrough was fail-OPEN: an unrecognised status
    escaped the gate silently. A gate guarding a completion claim must fail CLOSED, so
    an unknown status is conservatively treated as ``unstarted`` (= gated, demands a
    disposition) rather than assumed in-flight."""
    s = (status or "").strip().lower()
    if s in _UNSTARTED_STATUSES:
        return "unstarted"
    if s in _DONE_STATUSES:
        return "done"
    if s in _CANCELLED_STATUSES:
        return "cancelled"
    if s in _STARTED_STATUSES:
        return "in_progress"
    # Unknown status → fail-closed: gated, so a typo / new vocabulary word cannot
    # slip an important task past the attainment gate unnoticed.
    return "unstarted"


def is_backlog_gated(task):
    """True if ``task`` is an UNSTARTED highest/high work item (ms-119 / e-4579).

    ``task`` is a work-item dict (``{"id", "type", "status", "meta": {"priority"}}``).
    Only ``type == "task"`` items with an unstarted status and a gated priority
    qualify — commits / notes / done / in-flight tasks do not. Pure."""
    if (task.get("type") or "task") != "task":
        return False
    if normalize_task_status(task.get("status")) != "unstarted":
        return False
    priority = ((task.get("meta") or {}).get("priority") or "").strip().lower()
    return priority in BACKLOG_GATED_PRIORITIES


def append_disposition(entry, *, task_id, verdict, reason, source, actor, at):
    """Record a disposition for one backlog task onto a pending approval (e-4579).

    Each record answers "what happened to this unstarted highest/high task?" with a
    value from ``DISPOSITION_VERDICTS``. ``superseded`` REQUIRES a non-empty reason
    (dropping important work must be justified in the record, not asserted). Like
    ``append_review_evidence``, ``source`` is the self-declared provenance (recorded
    verbatim, NOT structurally proven independent) — the value is that a disposition
    cannot be applied SILENTLY and its provenance is grep-able.

    Append-only: the latest record for a task_id wins at read time
    (``disposition_map``), but every attempt stays on the ledger for audit. Pure —
    the caller owns persistence."""
    if verdict not in DISPOSITION_VERDICTS:
        raise ValueError(
            "invalid disposition verdict %r (expected one of %s)"
            % (verdict, ", ".join(DISPOSITION_VERDICTS)))
    if verdict == "superseded" and not (reason or "").strip():
        raise ValueError(
            "disposition 'superseded' requires a --reason (why the task can be "
            "dropped without blocking attainment)")
    if not (task_id or "").strip():
        raise ValueError("disposition requires a task_id")
    rec = {
        "task_id": task_id,
        "verdict": verdict,
        "reason": reason or "",
        "source": source or "",
        "actor": actor,
        "at": at,
    }
    entry.setdefault("meta", {}).setdefault("dispositions", []).append(rec)
    return entry


def disposition_map(entry):
    """Latest disposition per task_id on ``entry`` (ms-119 / e-4579).

    Returns ``{task_id: record}`` keeping the LAST appended record for each task
    (append-only ledger; latest wins). Empty dict when none recorded. Pure."""
    out = {}
    for rec in (entry.get("meta") or {}).get("dispositions", []) or []:
        tid = rec.get("task_id")
        if tid:
            out[tid] = rec
    return out


def undisposed_backlog(entry, backlog):
    """The gated tasks that still lack a disposition (ms-119 / e-4579).

    ``backlog`` is the list of unstarted highest/high task dicts for the target
    (collected mechanically by the caller — see core.unstarted_priority_tasks).
    A task is considered disposed when EITHER it now has an explicit disposition
    record on the approval, OR its live status is already done/cancelled (an
    implicit disposition — the concern the gate protects against, a silent skip,
    cannot apply to work that is visibly finished or dropped).

    Returns the sub-list of ``backlog`` whose ids are still undisposed — empty
    means the gate is satisfied. Pure — takes primitive lists so it is trivially
    testable and cannot drift from the persistence layer."""
    disposed = set(disposition_map(entry).keys())
    out = []
    for task in backlog:
        tid = task.get("id")
        if tid in disposed:
            continue
        if normalize_task_status(task.get("status")) in ("done", "cancelled"):
            continue
        out.append(task)
    return out


def blocks_attainment_backlog(entry, backlog):
    """Gated tasks whose LATEST disposition is ``blocks-attainment`` (e-4579, #551).

    ``blocks-attainment`` is the record that the task is still required and NOT done —
    i.e. the attainment claim is, on that record, false. Merely HAVING a disposition
    must not satisfy the gate (a task literally named "blocks attainment" would
    otherwise let approve pass): this surfaces those tasks so the approve path can
    refuse them (or demand an explicit ack), making "blocks-attainment がある間は
    attained にできない" structural rather than advisory.

    Returns the sub-list of ``backlog`` tasks whose latest disposition verdict is
    ``blocks-attainment``. Empty means none block. Pure."""
    disposed = disposition_map(entry)
    out = []
    for task in backlog:
        rec = disposed.get(task.get("id"))
        if rec and rec.get("verdict") == "blocks-attainment":
            out.append(task)
    return out


def stale_done_dispositions(entry, backlog):
    """Gated tasks disposed ``done`` on the ledger but still UNSTARTED live (e-4579).

    A ledger/reality divergence: the disposition says "the work was in fact
    completed", but the task's live status never left todo. That is exactly the
    "掃除機がある≠掃除した" gap the gate exists to surface, so the approve path warns
    (not refuses — the disposition is a good-faith claim; the fix is a one-liner) and
    prints a ``beacon task done <id>`` recovery hint.

    Returns the sub-list of ``backlog`` tasks with a latest disposition of ``done``
    whose live status is still ``unstarted``. Pure."""
    disposed = disposition_map(entry)
    out = []
    for task in backlog:
        rec = disposed.get(task.get("id"))
        if not rec or rec.get("verdict") != "done":
            continue
        if normalize_task_status(task.get("status")) == "unstarted":
            out.append(task)
    return out


def format_blocks_attainment(blocking, *, target_id):
    """Render the "these gated tasks are marked blocks-attainment" refusal block.

    Returns "" when ``blocking`` is empty. Names each task + priority so the human sees
    exactly which recorded blocker keeps the attainment claim from being approvable."""
    if not blocking:
        return ""
    lines = [
        f"⛔ blocks-attainment のタスクが残っています ({target_id}) — "
        f"attained にはできません:",
    ]
    for task in blocking:
        pri = ((task.get("meta") or {}).get("priority") or "").strip() or "?"
        desc = (task.get("description") or "").strip()
        if len(desc) > 60:
            desc = desc[:57] + "..."
        lines.append(f"  - {task.get('id')} [{pri}] {desc}")
    lines.append(
        "  各タスクを完了 (beacon task done <id>) して disposition を done に更新するか、"
        "superseded[理由必須] に切り替えてください。")
    return "\n".join(lines)


def format_stale_done_warning(stale, *, target_id):
    """Render a NON-blocking warning for tasks disposed ``done`` but still unstarted.

    Returns "" when ``stale`` is empty. Prints the recovery hint so the台帳(done) と
    実状態(todo) の乖離 is visible and one command away from fixed."""
    if not stale:
        return ""
    lines = [
        f"⚠ disposition=done だが live status が未着手のタスク ({target_id}) — "
        f"台帳と実状態が乖離しています:",
    ]
    for task in stale:
        lines.append(f"  - beacon task done {task.get('id')}")
    return "\n".join(lines)


def authored_timestamp_tiebreaker(task_created_at, spec_updated_at):
    """Last-written-intent tie-breaker for a task↔SPEC contradiction (ms-119 / e-4597).

    When the attainment review identifies that a gated task CONTRADICTS the target's
    SPEC (a *semantic* judgement made by the judge/human — NOT by this function), the
    disposition needs a principled tie-breaker: the LAST WRITTEN intent wins. This
    compares the task's ``created_at`` against the SPEC doc's ``updated_at`` and reports
    which is newer + which way the disposition leans, as EVIDENCE for that decision:

      - SPEC newer  → the SPEC re-scoped after the task was filed → the task is a
        ``superseded`` candidate (lean = "superseded").
      - task newer  → the task post-dates the SPEC → the task's intent stands and still
        needs a real disposition (lean = "task-valid").

    Motivating incident (ms-128 AC4): a vocab-unify task (filed 2026-07-27) was
    de-scoped by SPEC v2 (2026-07-28), but a judge partial-judged on the stale task
    criterion and nearly blocked attainment on an obsolete requirement. Symmetric so it
    also protects the other direction (a task newer than the SPEC is not silently
    superseded).

    Returns {spec_newer, lean, task_created_at, spec_updated_at} or None when either
    timestamp is missing/unparseable (surface no hint rather than a wrong one).

    CRITICAL — a TIE-BREAKER, never a blind auto-supersede:
      * the *detection* of a contradiction is semantic (judge/human); this fires only
        as evidence once a human/judge is deciding the disposition;
      * doc-level ``updated_at`` is COARSE — there is no per-section provenance, so an
        edit to an unrelated SPEC section makes the whole doc look "newer". A newer SPEC
        timestamp is therefore evidence to weigh, not proof the task is stale.
    Pure (parses the two given strings; no clock read)."""
    from datetime import datetime

    def _parse(ts):
        if not ts or not isinstance(ts, str):
            return None
        s = ts.strip().replace("Z", "+00:00")
        for candidate in (s, s[:19]):  # full ISO, then second-precision fallback
            try:
                # compare naive: a tz-present/absent mix across our own timestamps is
                # noise at the day-grain this coarse tie-breaker operates on.
                return datetime.fromisoformat(candidate).replace(tzinfo=None)
            except ValueError:
                continue
        return None

    t = _parse(task_created_at)
    s = _parse(spec_updated_at)
    if t is None or s is None:
        return None
    spec_newer = s > t
    return {
        "spec_newer": spec_newer,
        "lean": "superseded" if spec_newer else "task-valid",
        "task_created_at": task_created_at,
        "spec_updated_at": spec_updated_at,
    }


def format_backlog_gap(undisposed, *, target_id, spec_updated_at=None):
    """Render the "unstarted important tasks still need a disposition" block.

    Returns "" when ``undisposed`` is empty (caller omits the section). This is the
    forcing-function surface: it names each blocking task + its priority so the
    human/judge sees the concrete backlog the attainment claim is skipping over.

    ``spec_updated_at`` (ms-119 / e-4597): the target's SPEC doc ``updated_at``. When
    given, each task also gets a last-written-intent tie-breaker line (task.created_at
    vs the SPEC timestamp) so a judge/human deciding whether the task is superseded by a
    re-scoped SPEC has the evidence inline. Advisory — the disposition is still explicit
    and required; None (no SPEC / unresolvable) simply omits the tie-breaker."""
    if not undisposed:
        return ""
    verdicts = "|".join(DISPOSITION_VERDICTS)
    lines = [
        f"⚠ 未着手の重要タスク ({target_id}) — attainment 承認前に明示 disposition が必要:",
    ]
    showed_tiebreaker = False
    for task in undisposed:
        pri = ((task.get("meta") or {}).get("priority") or "").strip() or "?"
        desc = (task.get("description") or "").strip()
        if len(desc) > 60:
            desc = desc[:57] + "..."
        lines.append(f"  - {task.get('id')} [{pri}] {desc}")
        # ms-119 / e-4597: last-written-intent tie-breaker, surfaced as EVIDENCE for the
        # disposition when a SPEC timestamp is available. Framed "矛盾時の" — it only
        # applies IF the judge/human finds a contradiction; it is never auto-applied.
        if spec_updated_at:
            tb = authored_timestamp_tiebreaker(task.get("created_at"), spec_updated_at)
            if tb:
                showed_tiebreaker = True
                tdate = (task.get("created_at") or "")[:10]
                sdate = (spec_updated_at or "")[:10]
                if tb["spec_newer"]:
                    lines.append(
                        f"      ↳ 矛盾時の tie-breaker: SPEC({sdate}) が task({tdate}) より"
                        f"新しい → last-written-intent は SPEC 側 = superseded 候補")
                else:
                    lines.append(
                        f"      ↳ 矛盾時の tie-breaker: task({tdate}) が SPEC({sdate}) より"
                        f"新しい → タスク側の意図が有効 = done/blocks-attainment で disposition")
    lines.append(
        f"  各タスクに: beacon target attach-disposition <entry-id> "
        f"--task <task-id> --disposition <{verdicts}> [--reason <text> (superseded 時必須)]")
    lines.append(
        "  (superseded は理由必須。disposition は独立 judge / 人間承認を経て確定 — "
        "実装者の自己申告では attained にできません。)")
    if showed_tiebreaker:
        lines.append(
            "  ※ tie-breaker は『矛盾が検出された時』の後勝ち裁定のみ (矛盾の検出と最終判断は "
            "judge/人間)。doc 単位 updated_at は粗く、無関係 section の変更でも SPEC が新しく"
            "見えうるため、blind auto-supersede せず必ず確認すること。")
    return "\n".join(lines)


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
