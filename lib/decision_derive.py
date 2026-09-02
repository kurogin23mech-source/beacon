"""ms-166 e-5972 — derive decision-arm records from artifacts that already carry
the "why".

The 作り替え direction 2 (SPEC): a judgment trail should be a DERIVED product of
what teams already write — PR intent, commit rationale, task done_reason, review
findings — not a separate ``beacon decision record`` the AI must remember to call.
This mirrors the deliverable-changelog precedent (ms-161: a deliverable is a derived
product of the log, not hand-recorded).

Scope of THIS module: **PR intent** — the primary "why" artifact not yet on the
decision arm (task done_reason is already welded via the task-done seam; review
adjudication via e-5971; completion verdict via e-5978). A PR's intent IS a declared
decision: "we decided to make change X, because Y." We normalize it into a
``pr-intent`` decision, both write-through (at ``beacon pr add``) and by backfill
(``beacon decision derive`` over existing PRs).

Boundary (SPEC): a conversation's pure introspective judgment that was never written
into any artifact is out of scope — it does not reach a seam and cannot be derived.
Deriving invents no "why": an intent-less PR yields no decision (empty = honest
"no grounds", not a fabricated one — same principle as decision_event evidence).

Pure module: no I/O. The caller (cmd_pr write-through / cmd_decision derive) owns the
cloud POST via ``commands_shared.best_effort_decision_write``.
"""
from __future__ import annotations

DERIVED_PR_INTENT_KIND = "pr-intent"

# Cap on the existing-decision scan the backfill reads to build its dedup set.
# Idempotency holds only when this read covers ALL existing pr-intent decisions;
# past this many, the backfill must warn / abort rather than silently re-derive
# (AX/maintainability review of e-5972). Named so the limit is visible, not magic.
DEDUP_SCAN_LIMIT = 2000


def normalize_pr_number(pr_number) -> str:
    """PR number → canonical str dedup key (ms-166 e-5972, maintainability M4).

    The two callers feed different types — write-through passes the ``str`` from
    ``_pr_number_from_url``, backfill passes the ``int`` from ``meta.pr_number`` —
    so the dedup key (``covered_pr_numbers``) and the derivation must agree on ONE
    type or the same PR looks uncovered and re-derives. Normalize to ``str`` here,
    the single place both paths pass through. Empty / None → ``""``."""
    if pr_number is None:
        return ""
    return str(pr_number).strip()


def build_pr_intent_decision(pr_number, title: str, intent: str, *,
                             decided_by: str):
    """Build the ``pr-intent`` decision payload from a PR's declared intent, or
    ``None`` when it cannot be derived idempotently.

    ``decision`` (what) = the change (PR title); ``rationale`` (why) = the stated
    intent; ``evidence`` links the PR (``pr:<n>``) so "which decision for PR N" is
    queryable AND so the derivation is idempotent (``covered_pr_numbers`` reads it
    back). Returns ``None`` when either the intent is empty (we never fabricate a
    "why") OR the PR number is missing (without a dedup key, re-running would
    duplicate — an un-numbered PR is not derivable, AX/maintainability review)."""
    intent = (intent or "").strip()
    pr = normalize_pr_number(pr_number)
    if not intent or not pr:
        return None
    return {
        "kind": DERIVED_PR_INTENT_KIND,
        "decision": (title or "").strip() or f"PR#{pr}",
        "rationale": intent,
        "decided_by": decided_by,
        "evidence": [f"pr:{pr}"],
    }


def iter_pr_intent_artifacts(data: dict):
    """Yield ``(pr_number, title, intent)`` for every PR entry that carries a
    non-empty intent — the backfill source.

    PRs are recorded under milestones (``core.pr_add`` → ``find_target_milestone``),
    so we walk ``data['milestones'][*]['entries']`` for ``type == 'pr'``. Pure read;
    the caller decides which are already on the arm (dedup) and posts the rest."""
    for ms in (data.get("milestones") or []):
        for entry in (ms.get("entries") or []):
            if entry.get("type") != "pr":
                continue
            meta = entry.get("meta") or {}
            intent = (meta.get("intent") or "").strip()
            if not intent:
                continue
            yield (meta.get("pr_number"), entry.get("description") or "", intent)


def covered_pr_numbers(existing_decisions) -> set:
    """The set of PR numbers already on the arm as ``pr-intent`` decisions,
    read from each decision's ``evidence`` (``pr:<n>``). Used by backfill to skip
    PRs already derived (idempotency), so re-running ``derive`` never duplicates."""
    covered: set = set()
    for d in (existing_decisions or []):
        if (d.get("kind") or "") != DERIVED_PR_INTENT_KIND:
            continue
        for ev in (d.get("evidence") or []):
            if isinstance(ev, str) and ev.startswith("pr:"):
                covered.add(ev[len("pr:"):])
    return covered
