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
a backfill task (e-3625/e-3695) migrates stored data once tolerant readers are
deployed everywhere — run per project via ``beacon migrate target-labels``,
which stamps the ``BACKFILL_MARKER`` execution trail — and a later contract
task (e-3626) drops the legacy fallback once ``target_labels_backfilled`` is
true for all data and no legacy-key client remains.

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
# Target-advancement macro-frame — the class-layer, occupation-agnostic reason
# a Target/phase model exists at all: to make the AI agent read a Target as a
# thing to *push forward*, not a bucket to sit in. "仕事を進める" = advancing
# the currently-open Target to its next phase/state.
#
# This is the generalisation of the sales-only ``opportunity_phase_frame``
# (``sales_entities`` e-3582) to EVERY occupation. The sales version teaches how
# to read the opportunity *funnel*; this class-layer version teaches the same
# forward-motion reading for Targets in general (milestone / opportunity /
# operation / trek …). It rides on ``beacon status`` output so every project's
# AI is reminded inline each session — a per-project CORE doc would only reach
# THIS repo's sessions (ms-109 e-3751; CORE doc 0drGJ9f3UaKO7p0RrKQD 判断軌跡).
#
# The concept ("a target-class carries a macro-frame") is class-layer; the TEXT
# is a shipped default overridable per project via ``target_advancement_frame``
# in project.json (mirrors how ``opportunity_phase_frame`` is seeded+editable).
# ---------------------------------------------------------------------------

TARGET_ADVANCEMENT_FRAME = (
    "target (= 進める対象: milestone / opportunity / operation / trek 等) は、"
    "座って眺める箱ではなく前へ進めるものです。仕事を進めるとは、いま進行中の "
    "target のフェーズ / 状態を次の段階へ前進させること。滞留は『放置してよい』"
    "ではなく『まだ前進していない』状態を意味します。どの操作も最後に「この "
    "target を次に前進させる一手」を意識してください。"
)


def target_advancement_frame(data: dict) -> str:
    """The class-layer forward-motion frame text (ms-109 e-3751): how the AI
    should read every Target — as a stage to advance *out of*, not a bucket to
    rest in. Reads a project's configured ``target_advancement_frame`` (editable
    per project), else the shipped default ``TARGET_ADVANCEMENT_FRAME``."""
    return (data.get("target_advancement_frame") or "").strip() or \
        TARGET_ADVANCEMENT_FRAME


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


# ---------------------------------------------------------------------------
# Target linkage — the canonical ``target`` frontmatter key generalising doc
# linkage (ms-109 e-3754). Documents historically linked to a Target via one
# of three HARDCODED keys — ``milestone`` / ``operation`` / ``trek_id`` — a set
# frozen in the development era. Sales Targets (Account / Opportunity), added
# later (ms-106/107), got no linkage key at all, so the asymmetry "docs link to
# milestones but not customers" is just a wiring gap, not a design intent.
#
# The fix is one canonical ``target`` key holding a single prefixed id
# (``acc-1`` / ``opp-3`` / ``ms-5`` / ``op-2`` / ``tk-…``); the target-class is
# DERIVED from the id prefix, so no new key is needed when a Target class is
# added. ``doc_target`` reads canonical-first, legacy-second (expand step) so
# existing docs resolve with no data migration.
# ---------------------------------------------------------------------------

TARGET_LINK = "target"

# Id-prefix → target-class. Prefixes are the substring before the first "-"
# (``opp-3`` → ``opp``), so ``op-`` (operation) and ``opp-`` (opportunity) do
# not collide.
_TARGET_PREFIX_KIND = {
    "ms": "milestone",
    "op": "operation",
    "opp": "opportunity",
    "acc": "account",
    "acq": "acquisition",
    "tk": "trek",
}

# Legacy doc-linkage keys, in tolerant-read order, for Targets that predate the
# canonical ``target`` key. Sales classes (account / opportunity) never had one.
_LEGACY_LINK_KEYS = ("milestone", "operation", "trek_id")

# Target-class → its legacy doc-linkage key, for dual-writing during the
# expand→contract window so legacy readers (operation SPEC discovery, the
# ``--ms`` doc filter) keep working.
_KIND_LEGACY_LINK_KEY = {
    "milestone": "milestone",
    "operation": "operation",
    "trek": "trek_id",
}


def target_kind(target_id: str) -> str:
    """Derive a Target's class from its id prefix (``acc-1`` → ``account``,
    ``opp-3`` → ``opportunity``, ``ms-5`` → ``milestone`` …). Returns ``""`` for
    an empty id or an unrecognised prefix."""
    if not target_id or not isinstance(target_id, str):
        return ""
    prefix = target_id.split("-", 1)[0]
    return _TARGET_PREFIX_KIND.get(prefix, "")


def known_target_prefixes() -> tuple:
    """The canonical Target id-prefixes WITH the trailing ``-`` (``("acc-", "acq-",
    "ms-", …)``, sorted). Public accessor over the prefix→kind table so consumers
    (e.g. the capability-scope narrowing detector, e-5253) derive the prefix set
    without reaching the private ``_TARGET_PREFIX_KIND`` — a new class's prefix is
    covered the moment it lands in the table."""
    return tuple(sorted(p + "-" for p in _TARGET_PREFIX_KIND))


def doc_target(meta: dict) -> str:
    """Tolerant read of a document's linked Target id (ms-109 e-3754): canonical
    ``target`` first, then legacy ``milestone`` / ``operation`` / ``trek_id``
    (expand step). Returns ``""`` when the doc links to nothing."""
    if not isinstance(meta, dict):
        return ""
    v = meta.get(TARGET_LINK)
    if isinstance(v, str) and v.strip():
        return v.strip()
    for k in _LEGACY_LINK_KEYS:
        v = meta.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def legacy_link_key_for(target_id: str) -> str:
    """The legacy frontmatter key to dual-write for a Target id (back-compat),
    or ``""`` for classes (account / opportunity) that never had a legacy key."""
    return _KIND_LEGACY_LINK_KEY.get(target_kind(target_id), "")


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


def set_target_label(target: dict, label: str, *, dual_write: bool = True,
                     legacy_key: str = "") -> dict:
    """Set a Target's canonical ``label`` in place and return the target.

    During the version-skew window (before tolerant readers are deployed
    everywhere), the value is also mirrored onto the legacy key so older
    clients still read it. The legacy key is ``legacy_key`` when given, else
    the one already present on the target (``present_legacy_label_key``), else
    ``title`` as the safe default for a brand-new canonical-only target.

    ms-109 e-3697 (fable A-5): the default is ``dual_write=True`` — the SAFE
    side during the skew window. Previously the default was ``False`` (canonical
    only), so a single caller forgetting to pass ``dual_write=True`` would write
    a label old clients cannot read. Defaulting to True closes that "please
    remember the flag" footgun with a code default. Flip this default to
    ``False`` at the contract step (e-3626), once tolerant readers are deployed
    everywhere and the legacy key is being dropped; pass ``dual_write=False``
    explicitly before then only when the caller is certain no old reader exists.
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
# Backfill execution trail — the structural gate between the migrate step
# (e-3625/e-3695) and the contract step (e-3626).
#
# ``ensure_target_label`` is the per-record migrate unit; the occupation
# adapters wrap it in ``backfill_target_labels`` to sweep a whole project. But a
# sweep function that exists is not a sweep that RAN: the contract step must not
# drop the legacy fallback until it can PROVE every stored record was stamped
# (task-done-judgment-principle 原則6 — 機構を書いた≠達成した). So the CLI
# migrate verb records this marker after a successful sweep, and
# ``target_labels_backfilled`` is the predicate e-3626 gates on.
#
# Why the reader-deployed → backfill ordering (task e-3695 AC3) is structurally
# safe: the sweep is ADDITIVE — ``ensure_target_label`` writes the canonical
# ``label`` and never removes the legacy ``title`` / ``name``. So no reader,
# old (legacy-only) or new (tolerant), can be broken by running it. The
# ordering hazard lives entirely in the CONTRACT step (dropping the legacy
# key), which is why the gate belongs there: e-3626 requires both this marker
# (data was swept) AND a confirmation that no legacy-key client remains.
# ---------------------------------------------------------------------------

BACKFILL_MARKER = "target_labels_backfill"


def stamp_target_labels_backfill(data: dict, *, dev_count: int,
                                 sales_count: int, version: str = "",
                                 at: str = "") -> dict:
    """Record on the project ``data`` that the target-label backfill has run,
    in place, and return ``data``. Stores the timestamp, the beacon version that
    ran it (so the contract step can confirm the sweep happened at or after the
    version where tolerant readers shipped), and how many records each
    occupation adapter stamped. ``at`` / ``version`` fall back to
    ``work_base.now_iso()`` / ``""`` when blank."""
    data[BACKFILL_MARKER] = {
        "at": at or work_base.now_iso(),
        "version": version,
        "dev_count": dev_count,
        "sales_count": sales_count,
    }
    return data


def target_labels_backfilled(data: dict) -> bool:
    """True when the target-label backfill (e-3695) has been recorded as run
    over this project. The contract step (e-3626) MUST gate on this before it
    drops the legacy fallback — a project whose stored records were never
    stamped with canonical ``label`` would otherwise lose its labels the moment
    the legacy read is removed."""
    if not isinstance(data, dict):
        return False
    marker = data.get(BACKFILL_MARKER)
    return isinstance(marker, dict) and bool(marker.get("at"))


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


# ---------------------------------------------------------------------------
# Ball — whose court the work is in. Part of the thick cognitive frame every
# occupation shares (ms-124 e-4089): sales names it who_has_the_ball on an
# Opportunity; development leaves it implicit (a task in flight is always "ours").
# The canonical vocabulary lives here so a data-defined target-class inherits the
# same "whose turn is it" concept as code-defined ones rather than re-inventing a
# thinner one. sales_entities keeps its own historical copy (behavior-preserving,
# not refactored here); new descriptor-driven targets read these.
# ---------------------------------------------------------------------------

BALL_SELF = "self"            # our turn — we owe the next move
BALL_COUNTERPART = "counterpart"  # the other party's turn — we are waiting on them
BALL_NONE = ""                # no ball in play (target done / not yet started)
VALID_BALL = (BALL_SELF, BALL_COUNTERPART, BALL_NONE)

# The canonical RECORD FIELD KEY the ball lives under. The ball VALUES (self /
# counterpart / none) are the vocabulary above; this is the field name they are
# stored under on a target record. Named here — the occupation-agnostic layer
# that already owns the ball vocabulary — as the single source of truth so
# ``target_engine.BALL_KEY`` and ``target_state`` reference it instead of
# re-declaring the literal (ms-142 T2 maintainability review: two copies of
# "who_has_the_ball" could drift).
BALL_FIELD = "who_has_the_ball"


def normalize_ball(value: str) -> str:
    """Return ``value`` if it is a recognised ball state, else ``BALL_NONE``.
    Tolerant read: an unknown / missing ball never raises, it degrades to "no
    ball in play" so a projection over legacy data stays well-formed."""
    return value if value in VALID_BALL else BALL_NONE


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

    Canonical key is ``linked_id``, written by each occupation on the same
    field (ms-109 e-3560 / e-3696): a development commit stamps it through
    ``link_evidence`` (in ``core.log_commit``); a sales communication
    records it directly in ``sales_entities.communication_add``'s builder (kept
    inline there so a target-level communication can carry an explicit empty
    ``linked_id``). Both land on ``linked_id``, so this accessor reads the same
    field for both — the tolerant ``.get`` also keeps working for older records
    that predate the stamp. (The close half — marking the linked work item done
    — is unified through ``mark_done``; see ``close_work_item_with_evidence``.)
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
    ``reason``). Returns ``(work_item, evidence)``.

    This is an OPTIONAL composition of the two primitives that ARE wired into
    production separately (ms-109 e-3696): the link half (``link_evidence``) is
    used by ``core.log_commit``, and the done half (``mark_done``) by
    ``core.task_done`` / ``sales_entities.activity_set_status``. Both real close
    flows link and judge-done in DISTINCT steps — a dev commit records the task
    it resolves but lets the AI judge done later; a sales send records the
    communication and closes the fulfilled activity as two Skill steps — so
    neither calls this bundled form today. It stays as the one-call primitive
    for a future flow that genuinely has both the evidence and the work item in
    hand at once. Do NOT read "no current caller" as "unwired": its two halves
    are each wired (task-done-judgment-principle 原則6 — this note keeps the
    docstring honest about what actually runs)."""
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

    Wired into ``core.milestone_add`` (ms-109 e-3698 / fable A-4): the
    development add-path builds its dict through here, so the base owns the
    generic skeleton (id / label / status / created_at / created_by / assignee)
    and its defaults for milestones.

    STAGED for sales — ``account_add`` / ``opportunity_add`` do NOT route
    through here yet. This is an intentional staged step, not an oversight
    (SPEC 方針3, "まず具体で検証してから抽象へ"): the sales model is still moving
    and diverges from the generic skeleton — an Account has no generic
    ``status`` (継続 / never-terminal, tracked by phase) and an Opportunity's
    ``status`` is phase-derived rather than the base default. Forcing them
    through this constructor now would inject skeleton the sales model hasn't
    settled on. They apply the base *defaults* piecemeal (e.g. ``created_at``)
    and full routing is the remaining extraction. Per
    task-done-judgment-principle 原則6, do not read "generic constructor exists"
    as "all add-paths unified" — only the milestone path is.

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
