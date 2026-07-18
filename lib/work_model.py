"""Beacon work-model — the occupation-agnostic canonical shape for the three
work concepts every instance shares: Target, WorkItem, Evidence. ms-109 e-3559.

Builds on ``work_base.py`` (the invariant primitives: ids, actor, cancel,
audit). Where the two existing instances — ``core.py`` (development:
milestone / task / commit) and ``sales_entities.py`` (sales: account /
opportunity / activity / communication) — diverge on the FIELD NAME they use
for the same generic concept, this module defines the CANONICAL key plus a
TOLERANT accessor that reads canonical-first, legacy-second.

The canonical divergence handled here:
  - Target display label: canonical ``label``; legacy ``title`` (milestone /
    opportunity) or ``name`` (account).
  - WorkItem done stamp: canonical ``done_at`` (development already has it;
    sales activity historically had none → reads as ``None``).

This is the *expand* step of an expand → migrate → contract field unification
(SPEC ``p8bNPiWdlVW0lfjiBWqg`` 判断軌跡; memo ``q1TYiFBAbbf0g9Qb1zGB``, C-target
adopted 2026-07-17). Readers become occupation-agnostic immediately via the
tolerant accessor, WITHOUT a big-bang data migration: new writes are canonical
(with an optional dual-write to the legacy key during the version-skew window),
a later backfill task (e-3625) migrates stored data once tolerant readers are
deployed everywhere, and a later contract task (e-3626) drops the legacy
fallback once all data and clients are on canonical keys.

Occupation SEMANTICS (phase, who_has_the_ball, pipeline, resolves, channel) are
deliberately NOT here — they stay in each instance's adapter, per SPEC AC4
("基底コードは職種非依存"). Only the generic skeleton (label / status /
created_at / done_at / assignee / linked_id) is unified.

Like ``work_base.py`` and ``core.py`` this module performs no I/O: every
function is a pure transform over the values it is handed.
"""

from __future__ import annotations

from typing import Optional

import work_base


# ---------------------------------------------------------------------------
# Status vocabulary — the generic lifecycle states shared by every instance.
# ``cancelled`` is owned by work_base (the single cancel vocabulary); done/todo
# are re-exported here so occupation code has one place to read them from.
# ---------------------------------------------------------------------------

TODO_STATUS = "todo"
DONE_STATUS = "done"
CANCELLED_STATUS = work_base.CANCELLED_STATUS  # "cancelled"


# ---------------------------------------------------------------------------
# Target label — canonical ``label`` with a tolerant read over legacy keys.
#
# A Target is the aggregate a work instance drives (development milestone,
# sales opportunity / account). Its human display name lives under different
# keys today; ``target_label`` reads them uniformly so no reader needs to know
# which occupation it is looking at.
# ---------------------------------------------------------------------------

LABEL = "label"

# Legacy label keys, tried in order when canonical ``label`` is absent.
# ``title`` covers development milestones and sales opportunities; ``name``
# covers sales accounts.
_LEGACY_LABEL_KEYS = ("title", "name")


def target_label(target: dict) -> str:
    """Return a Target's display label, occupation-agnostic.

    Reads the canonical ``label`` first, then falls back to legacy ``title`` /
    ``name`` (in that order). Returns ``""`` when the target is not a dict or
    carries no label under any known key. This is the tolerant read that lets
    the UI / dashboard / session-start / map / retrospect show a target's name
    without branching on whether it is a milestone, opportunity, or account.
    """
    if not isinstance(target, dict):
        return ""
    v = target.get(LABEL)
    if v:
        return v
    for k in _LEGACY_LABEL_KEYS:
        v = target.get(k)
        if v:
            return v
    return ""


def present_legacy_label_key(target: dict) -> str:
    """Return whichever legacy label key (``title`` / ``name``) already carries
    a value on ``target``, or ``""`` if none does.

    Used to decide which legacy key a dual-write should mirror to, so an
    account keeps writing ``name`` and a milestone / opportunity keeps writing
    ``title`` without the base needing to know the occupation.
    """
    if not isinstance(target, dict):
        return ""
    for k in _LEGACY_LABEL_KEYS:
        if target.get(k):
            return k
    return ""


def set_target_label(target: dict, label: str, *, dual_write: bool = False,
                     legacy_key: str = "") -> dict:
    """Set a Target's canonical ``label`` in place and return the target.

    During the version-skew window (before tolerant readers are deployed
    everywhere), pass ``dual_write=True`` to also mirror the value onto the
    legacy key so older clients still read it. The legacy key is
    ``legacy_key`` when given, else the one already present on the target
    (``present_legacy_label_key``), else ``title`` as the safe default for a
    brand-new canonical-only target. ``dual_write=False`` (the default) writes
    canonical only — correct once the contract step (e-3626) is reached.
    """
    target[LABEL] = label
    if dual_write:
        lk = legacy_key or present_legacy_label_key(target) or "title"
        target[lk] = label
    return target


def ensure_target_label(target: dict) -> bool:
    """Idempotently backfill a Target's canonical ``label`` from its legacy key.

    Returns ``True`` when a canonical ``label`` was newly written, ``False``
    otherwise (already canonical, not a dict, or no label under any key). This
    is the per-target unit of the migrate step (e-3625): once every stored
    Target carries ``label``, the contract step (e-3626) can drop the legacy
    fallback. Occupation-agnostic — it reads through the tolerant
    ``target_label`` accessor, so it backfills a milestone / opportunity
    (legacy ``title``) and an account (legacy ``name``) with the same code and
    without knowing which occupation it is looking at. The legacy key is left
    untouched (this is additive; dropping it is e-3626's job).
    """
    if not isinstance(target, dict):
        return False
    if target.get(LABEL):
        return False
    legacy = target_label(target)
    if not legacy:
        return False
    target[LABEL] = legacy
    return True


# ---------------------------------------------------------------------------
# WorkItem lifecycle — status reads and the canonical done stamp.
#
# A WorkItem is a planned unit of work (development task, sales activity /
# nurturing). It carries an ``id``, a ``description``, a ``status``, and — once
# completed — a ``done_at`` timestamp. Cancel is handled by work_base
# (``stamp_cancel``); done and open/closed reads live here.
# ---------------------------------------------------------------------------

DONE_AT = "done_at"


def work_item_status(item: dict) -> str:
    """Return a WorkItem's status, or ``""`` when absent / not a dict."""
    if not isinstance(item, dict):
        return ""
    return item.get("status", "")


def is_done(item: dict) -> bool:
    """True when the WorkItem's status is the canonical done state."""
    return work_item_status(item) == DONE_STATUS


def is_cancelled(item: dict) -> bool:
    """True when the WorkItem's status is the canonical cancelled state."""
    return work_item_status(item) == CANCELLED_STATUS


def is_open(item: dict) -> bool:
    """True when the WorkItem is neither done nor cancelled — i.e. still live
    work. This is the occupation-agnostic "is there still a ball in play"
    predicate that both instances need (open tasks, open activities)."""
    return work_item_status(item) not in (DONE_STATUS, CANCELLED_STATUS)


def mark_done(item: dict, *, at: str = "", actor: str = "",
              reason: str = "") -> dict:
    """Mark a WorkItem done in place and return it.

    Sets ``status="done"`` and stamps the canonical ``done_at`` (``at`` falls
    back to ``work_base.now_iso()``). When ``reason`` or a non-default
    ``actor`` is given, they are recorded on ``meta.done_by`` / ``meta.
    done_reason`` so the completion stays attributable — mirroring how
    ``stamp_cancel`` records who/why for a cancel. Occupation-specific
    completion side effects (a commit resolving a task, a communication closing
    an activity — evidence-close, task e-3560) are NOT done here; this only
    moves the item's own lifecycle to done.
    """
    item["status"] = DONE_STATUS
    item[DONE_AT] = at or work_base.now_iso()
    if actor or reason:
        meta = item.setdefault("meta", {})
        meta["done_by"] = actor or work_base.current_actor()
        if reason:
            meta["done_reason"] = reason
    return item


# ---------------------------------------------------------------------------
# Evidence skeleton — the generic shape of an append-only record of something
# that happened (development commit, sales communication). The close mechanism
# (evidence closing a work item) is task e-3560; here we only carry the shared
# skeleton so both instances allocate the same generic fields.
# ---------------------------------------------------------------------------

LINKED_ID = "linked_id"


def evidence_linked_id(evidence: dict) -> str:
    """Return the id of the work item this Evidence closes, or ``""``.

    Canonical key is ``linked_id``. Both occupations now stamp it through
    ``link_evidence`` (e-3560): a sales communication records the activity /
    nurturing it fulfilled, and a development commit records the task it
    resolves. So this accessor reads the same field for both — the tolerant
    ``.get`` keeps working for older records that predate the dev-side stamp.
    """
    if not isinstance(evidence, dict):
        return ""
    return evidence.get(LINKED_ID, "")


def link_evidence(evidence: dict, work_item_id: str) -> dict:
    """Record on an Evidence which work item it closes, in place, and return it.

    This is the occupation-agnostic half of evidence-close (e-3560): a commit
    that resolves a task and a communication that fulfills an activity express
    the *same* relation — "this thing that happened closes that planned unit of
    work" — so both stamp it through one primitive under the canonical
    ``linked_id`` key. A falsy ``work_item_id`` is a no-op (the evidence closes
    nothing, e.g. a commit at milestone top level). Occupation-specific placement
    (nesting the commit under the task, the communication under the activity)
    stays in each instance's adapter; only the link field is unified here.
    """
    if work_item_id:
        evidence[LINKED_ID] = work_item_id
    return evidence


def close_work_item_with_evidence(work_item: dict, evidence: dict, *,
                                  at: str = "", actor: str = "",
                                  reason: str = "") -> tuple:
    """Close a WorkItem *with* the Evidence that closes it, occupation-agnostic.

    Performs the full evidence-close in one call: stamps the evidence's
    ``linked_id`` to point at ``work_item`` (via ``link_evidence``) and marks
    the work item done (via ``mark_done``, carrying ``at`` / ``actor`` /
    ``reason``). Returns ``(work_item, evidence)``. This is the base primitive
    for "a commit closes a task" and "a communication closes an activity" —
    the relation the whole ms-109 fold is built to share (SPEC AC2). Callers
    that only want the link (and let the AI judge done separately, as the dev
    commit flow does) use ``link_evidence`` alone.
    """
    link_evidence(evidence, work_item.get("id", ""))
    mark_done(work_item, at=at, actor=actor, reason=reason)
    return work_item, evidence


# ---------------------------------------------------------------------------
# Generic skeleton constructors — produce the shared base dict for each
# concept. Callers merge occupation-specific fields via ``extra`` (or after the
# call); the base only guarantees the generic skeleton and its defaults, so a
# new occupation reuses these instead of re-deriving the common fields.
# ---------------------------------------------------------------------------

def new_target(target_id: str, label: str, *, status: str = TODO_STATUS,
               created_at: str = "", created_by: str = "",
               assignee: str = "", **extra) -> dict:
    """Build the generic skeleton of a Target (canonical ``label``).

    Occupation-specific fields (a milestone's ``target_date`` / ``commits``,
    an opportunity's ``phase`` / ``account_id`` / ``amount``, an account's
    ``phase_history`` / ``contacts``) are passed via ``extra`` or added by the
    caller afterwards. ``created_by`` / ``created_at`` fall back to
    ``work_base`` when blank.
    """
    target = {
        "id": target_id,
        LABEL: label,
        "status": status,
        "created_at": created_at or work_base.now_iso(),
        "created_by": created_by or work_base.current_actor(),
        "assignee": assignee,
    }
    target.update(extra)
    return target


def new_work_item(item_id: str, description: str, *, status: str = TODO_STATUS,
                  created_at: str = "", **extra) -> dict:
    """Build the generic skeleton of a WorkItem (id / description / status /
    created_at). Occupation-specific fields (a task's ``date`` / ``meta`` /
    ``motivation``, an activity's ``deadline`` / ``who_has_the_ball`` /
    ``created_in_phase``) come via ``extra`` or from the caller."""
    item = {
        "id": item_id,
        "description": description,
        "status": status,
        "created_at": created_at or work_base.now_iso(),
    }
    item.update(extra)
    return item


def new_evidence(evidence_id: str, *, linked_id: str = "",
                 created_at: str = "", **extra) -> dict:
    """Build the generic skeleton of an Evidence record (id / created_at /
    canonical ``linked_id``). Occupation-specific fields (a commit's ``hash``,
    a communication's ``direction`` / ``channel`` / ``summary`` / ``source``)
    come via ``extra`` or from the caller."""
    evidence = {
        "id": evidence_id,
        LINKED_ID: linked_id,
        "created_at": created_at or work_base.now_iso(),
    }
    evidence.update(extra)
    return evidence


# ---------------------------------------------------------------------------
# Generic collection helpers — occupation-agnostic lookups over a flat list of
# records that each carry an ``id``. How records are gathered (a flat list, a
# recursive walk over nested entries) stays in the instance, because that shape
# is occupation-specific; these operate on the list once the caller has it.
# ---------------------------------------------------------------------------

def find_by_id(records, rec_id: str) -> Optional[dict]:
    """Return the first record in ``records`` whose ``id`` equals ``rec_id``,
    or ``None``. Non-dict entries are skipped."""
    for r in records or []:
        if isinstance(r, dict) and r.get("id") == rec_id:
            return r
    return None


def collect_ids(records) -> list:
    """Return the list of ``id`` values from ``records`` (skipping entries with
    no id). Handy for feeding ``work_base.next_suffixed_id``."""
    out = []
    for r in records or []:
        if isinstance(r, dict) and r.get("id"):
            out.append(r["id"])
    return out
