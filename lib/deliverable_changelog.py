"""The root target's deliverable-changelog — an append-type log of the value
each child target has PRODUCED (ms-161 e-5821 / SPEC 方針1-2).

Why this module exists
----------------------
ms-155 made ``deliverable`` (= 生み出した価値) a first-class arm, but implemented
it as a per-target **projection**: a target-class declared a ``doc`` / ``rollup``
projector + a ``ref`` pointer, and ``deliverable_resolve`` resolved the pointer to
a value. That projection is thin (SPEC 背景): the milestone deliverable is just a
``ref: "application-map"`` proxy pointer — no history (only a current snapshot),
no attribution (which work-item/evidence produced it), and asymmetric with the
other three arms (work-item / evidence / decision) which are all **append logs**.

ms-161 moves the axis: a target that produces value APPENDS one entry to the
ROOT target's deliverable-changelog. The log's active部分, summarised by category,
IS the current-state map (application-map becomes a *derived* view over this log —
e-5824/e-5825). This module is the foundation: the abstract entry schema plus the
root-level append/read seams. Lifecycle SEMANTICS (retire / supersede / the
active-only summary) build on the ``status``/``supersedes`` fields stored here but
are their own task (e-5822); the projector that summarises the log is e-5824.

Storage — root-owned, profession-independent
--------------------------------------------
The log lives on the project dict under ``CHANGELOG_KEY`` — a root-OWNED field
(like the ``root_narrative`` 大目的/経緯, but appendable), NOT inside any
occupation's collection (``milestones`` / ``opportunities`` / …). The key is a
generic token so the same log carries a dev project's produced capabilities, a
sales project's closed deals, a back-office project's readied processes — one
schema, rendered per profession later (SPEC 方針2 / 受入条件5). This is why the
module knows nothing about milestones: it operates on the abstract root arm.

Write discipline mirrors ``root_target``'s field seams (``set_root_label``):
``append_deliverable`` mutates ``data`` **in memory only** and returns the stored
entry; the CALLER owns persistence (``save_project``), so the cloud/local write
path and its audit tag are unchanged. Ids are minted deterministically
(``dlv-<N>`` via ``work_base.next_suffixed_id``) so a test can assert exact ids
without stubbing a clock or RNG.

Import layering: a leaf that depends only on ``work_base`` (id/time/actor
primitives). ``root_target`` sits above and may compose this; this module does
not import back, so there is no cycle.
"""
from __future__ import annotations

import work_base


# ---------------------------------------------------------------------------
# Storage key + schema vocabulary.
# ---------------------------------------------------------------------------

# The root-owned field the log lives under. A GENERIC token (not "milestones" /
# "deliverables_dev") so the schema stays profession-independent (受入条件5): a
# reader of any project — dev / sales / back-office — finds its produced value
# under the same key.
CHANGELOG_KEY = "deliverable_changelog"

# Minted-id prefix. ``dlv-`` (deliverable) — distinct from ``e-`` (work-item
# entries) / ``cl-`` (claims) so an id names its own kind at a glance.
_ID_PREFIX = "dlv-"

# Lifecycle statuses (SPEC 方針3). ``active`` entries are the ones a summary/map
# projector counts as CURRENT; ``superseded`` (replaced by a later entry) and
# ``retired`` (the capability was removed) fall out of the current-state view.
# The FILTER semantics (active-only summary, the retire/supersede operations)
# are e-5822 — this module only stores the field with a validated value so the
# lifecycle task has a well-formed column to act on.
STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"
STATUS_RETIRED = "retired"
DELIVERABLE_STATUSES = frozenset({STATUS_ACTIVE, STATUS_SUPERSEDED, STATUS_RETIRED})


class DeliverableValidationError(ValueError):
    """Raised when a raw deliverable entry is missing a required field or carries
    an out-of-vocabulary value. A ``ValueError`` subclass so a caller that only
    wants "bad input" can catch the base type, while a caller that wants to
    distinguish deliverable-schema failures from other value errors can catch
    this precise type."""


# ---------------------------------------------------------------------------
# Schema — the abstract deliverable-changelog entry (SPEC 方針2).
# ---------------------------------------------------------------------------

def _clean_str(value, field: str, *, required: bool) -> str:
    """Coerce ``value`` to a stripped string. A required field that is missing /
    blank / non-string raises; an optional one coalesces to ``""``."""
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise DeliverableValidationError(
            f"{field} must be a string (got {type(value).__name__})")
    if required and not text:
        raise DeliverableValidationError(f"{field} is required")
    return text


def _clean_source(raw) -> dict:
    """Validate the ``source`` attribution — WHICH target produced this value.
    Requires both ``target_id`` and ``kind`` so every produced value is traceable
    to a concrete target (the attribution the projection lacked). Returns a fresh
    dict carrying exactly those two keys."""
    if not isinstance(raw, dict):
        raise DeliverableValidationError(
            'source must be a dict with string keys "target_id" and "kind", '
            'e.g. {"target_id": "ms-42", "kind": "milestone"}')
    return {
        "target_id": _clean_str(raw.get("target_id"), "source.target_id",
                                 required=True),
        "kind": _clean_str(raw.get("kind"), "source.kind", required=True),
    }


def _clean_tags(raw) -> list:
    """Coerce ``tags`` to a list of non-empty strings (default ``[]``). A single
    string is NOT auto-split — callers pass a list — but ``None`` is tolerated so
    an omitted field is legal."""
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise DeliverableValidationError("tags must be a list of strings")
    out = []
    for t in raw:
        text = _clean_str(t, "tags[]", required=False)
        if text:
            out.append(text)
    return out


def _clean_status(raw) -> str:
    """Validate ``status`` against ``DELIVERABLE_STATUSES`` (default ``active``).
    An out-of-vocabulary value raises rather than silently defaulting, so a typo
    ('actve') surfaces at append time instead of quietly dropping the entry out
    of the current-state view later."""
    text = _clean_str(raw, "status", required=False) or STATUS_ACTIVE
    if text not in DELIVERABLE_STATUSES:
        raise DeliverableValidationError(
            f"status must be one of {sorted(DELIVERABLE_STATUSES)} (got {text!r})")
    return text


def normalize_deliverable_entry(raw: dict) -> dict:
    """Validate and canonicalise the CONTENT of one deliverable entry (SPEC 方針2).

    Pure: takes a raw dict, returns a fresh canonical dict, never mutates
    ``raw``. Validates the required attribution + description (``source`` /
    ``category`` / ``title`` / ``summary``) and coerces the optional
    drill-down/grouping/lifecycle fields (``ref`` / ``tags`` / ``status`` /
    ``supersedes``) to their defaults. Does NOT stamp provenance (``id`` / ``at``
    / ``actor``) — those are assigned at APPEND time against the live log, so this
    validator is independently reusable (a caller can shape-check an entry before
    deciding to append). Raises ``DeliverableValidationError`` on bad input.

    Field order in the returned dict follows the SPEC schema so a serialised entry
    reads top-down: attribution → description → drill-down → lifecycle."""
    if not isinstance(raw, dict):
        raise DeliverableValidationError(
            "a deliverable entry must be a dict")
    return {
        # attribution — which target produced this value
        "source": _clean_source(raw.get("source")),
        #束ねる軸の型トークン (capability/surface/outcome/asset…). A required,
        # profession-declared vocabulary token; kept a free string here (the
        # per-profession vocabulary lives in the projector, e-5824), but it must
        # be PRESENT so every entry is groupable.
        "category": _clean_str(raw.get("category"), "category", required=True),
        # description — the map's display material
        "title": _clean_str(raw.get("title"), "title", required=True),
        "summary": _clean_str(raw.get("summary"), "summary", required=True),
        # drill-down + grouping (optional)
        "ref": _clean_str(raw.get("ref"), "ref", required=False),
        "tags": _clean_tags(raw.get("tags")),
        # lifecycle (SPEC 方針3) — stored here, acted on in e-5822
        "status": _clean_status(raw.get("status")),
        "supersedes": _clean_str(raw.get("supersedes"), "supersedes",
                                 required=False) or None,
    }


# ---------------------------------------------------------------------------
# Append / read — the root target's deliverable-changelog arm (受入条件1).
# ---------------------------------------------------------------------------

def _log(data: dict) -> list:
    """Return the (possibly absent) changelog list on ``data`` WITHOUT creating
    it — the read side must not mutate. A malformed non-list value is treated as
    an empty log (defensive: a hand-edited project should not crash a read)."""
    log = data.get(CHANGELOG_KEY)
    return log if isinstance(log, list) else []


def _copy_entry(entry: dict) -> dict:
    """Deep-ish copy one stored entry for a read consumer: the top-level dict plus
    the two nested containers (``source`` / ``tags``) callers might mutate. Keeps
    the read side non-mutating for its consumers without a full ``deepcopy`` (the
    remaining values are immutable scalars)."""
    copy = dict(entry)
    if isinstance(entry.get("source"), dict):
        copy["source"] = dict(entry["source"])
    if isinstance(entry.get("tags"), list):
        copy["tags"] = list(entry["tags"])
    return copy


def _find_entry(data: dict, entry_id: str) -> dict | None:
    """Return the LIVE stored entry dict with ``entry_id`` (not a copy — lifecycle
    ops mutate it in place), or ``None`` if absent."""
    for entry in _log(data):
        if isinstance(entry, dict) and entry.get("id") == entry_id:
            return entry
    return None


def _mint_id(data: dict) -> str:
    """Mint the next ``dlv-<N>`` id over the ids already in the log — deterministic
    (max integer suffix + 1) so tests assert exact ids and concurrent-session
    collisions are the caller's save-time concern, not a random-id gamble."""
    existing = [e.get("id") for e in _log(data) if isinstance(e, dict)]
    return work_base.next_suffixed_id(existing, _ID_PREFIX)


def append_deliverable(data: dict, entry: dict, *,
                       at: str | None = None,
                       actor: str | None = None) -> dict:
    """Append one produced-value entry to the root's deliverable-changelog and
    return the stored entry (SPEC 方針1 / 受入条件1).

    ``entry`` is validated + canonicalised through ``normalize_deliverable_entry``
    first (so a bad entry raises BEFORE any mutation — append is all-or-nothing),
    then stamped with a minted ``id`` and the log-time provenance (``at`` / actor).
    ``at`` / ``actor`` default to ``now_iso()`` / ``current_actor()`` but are
    injectable so a test pins them and a backfill can carry a historical stamp.

    IN-MEMORY only: mutates ``data[CHANGELOG_KEY]`` (creating the list on first
    append) and returns a COPY of the stored entry. The CALLER owns persistence —
    mirroring ``root_target.set_root_label`` — so this module stays I/O-free and the
    cloud/local save path is unchanged. A COPY (not the live stored dict) is returned
    so a caller that mutates the result cannot corrupt the log out-of-band —
    symmetric with the read side (``read_deliverables`` / ``active_deliverables``);
    the log is mutable ONLY through this module's API (ms-161 AX review PR#694)."""
    normalized = normalize_deliverable_entry(entry)
    stamped = {
        "id": _mint_id(data),
        **normalized,
        "at": at if at is not None else work_base.now_iso(),
        "actor": actor if actor is not None else work_base.current_actor(),
    }
    log = data.get(CHANGELOG_KEY)
    if not isinstance(log, list):
        log = []
        data[CHANGELOG_KEY] = log
    log.append(stamped)
    return _copy_entry(stamped)


def read_deliverables(data: dict, *,
                      status: str | None = None,
                      category: str | None = None,
                      source_target: str | None = None) -> list:
    """Read the root's deliverable-changelog, newest-append last (insertion
    order), with optional filters (受入条件1 read side).

    - ``status`` — keep only entries with this lifecycle status. An unknown status
      value raises, symmetric with append-time validation, so a filter typo is loud.
      NOTE: ``read_deliverables(status=STATUS_ACTIVE)`` is a RAW status filter — it
      is NOT the current-state view. For "what the project can do now" use
      ``active_deliverables`` instead: it additionally drops a predecessor that a
      live successor supersedes (the ``supersedes`` exclusion), which a pure status
      filter does not. Using this filter where you meant ``active_deliverables``
      silently double-counts evolved capabilities.
    - ``category`` / ``source_target`` — keep only entries in that category / from
      that producing target.

    Returns fresh copies (a shallow copy per entry, with ``source`` copied too) so
    a caller cannot mutate the stored log through the returned rows — the read
    side is non-mutating even for its consumers."""
    if status is not None and status not in DELIVERABLE_STATUSES:
        raise DeliverableValidationError(
            f"status filter must be one of {sorted(DELIVERABLE_STATUSES)} "
            f"(got {status!r})")
    out = []
    for entry in _log(data):
        if not isinstance(entry, dict):
            continue
        if status is not None and entry.get("status") != status:
            continue
        if category is not None and entry.get("category") != category:
            continue
        if source_target is not None and \
                (entry.get("source") or {}).get("target_id") != source_target:
            continue
        out.append(_copy_entry(entry))
    return out


# ---------------------------------------------------------------------------
# Lifecycle (SPEC 方針3) — the status/supersedes machinery that makes "要約 =
# 現在地" true (ms-161 e-5822 / 受入条件2).
#
# e-5821 gave every entry a ``status`` column and a ``supersedes`` pointer but
# only STORED them. A pure append log grows without bound, so a naive summary
# would keep listing capabilities that were removed — the map would never "消す".
# This section is the "足す＆消す" (add & remove) abstraction of application-map's
# reconcile: two operations that move an entry OUT of the current-state set
# (``retire`` = the capability is gone; ``supersede`` = a newer entry replaces
# it), plus ``active_deliverables`` — the derived current-state view the map
# projector (e-5824) summarises.
#
# Status transitions mutate the entry IN PLACE (like the rest of the codebase's
# ``set_entry_state``), stamping WHEN/WHO for traceability (data-immutability
# principle: every state change is auditable). In-memory only — the caller owns
# persistence, matching ``append_deliverable``.
# ---------------------------------------------------------------------------

def _stamp_transition(entry: dict, status: str, at: str | None,
                      actor: str | None) -> None:
    """Move ``entry`` to ``status`` and stamp the transition provenance
    (``status_changed_at`` / ``status_changed_by``) so a reader can audit WHEN a
    capability left the current-state set and WHO recorded it — the original
    append ``at``/``actor`` are preserved (they record creation, not retirement)."""
    entry["status"] = status
    entry["status_changed_at"] = at if at is not None else work_base.now_iso()
    entry["status_changed_by"] = (
        actor if actor is not None else work_base.current_actor())


def retire_deliverable(data: dict, entry_id: str, *,
                       reason: str = "",
                       at: str | None = None,
                       actor: str | None = None) -> dict:
    """Retire a produced-value entry — the capability/outcome it recorded no
    longer exists, so it drops out of the current-state summary ("消す").

    Flips the entry's status to ``retired`` in place and stamps the transition;
    an optional ``reason`` (why it was removed) is recorded when given. Returns a
    COPY of the mutated entry (showing the new status) — NOT the live stored dict,
    so a caller mutating the result cannot corrupt the log out-of-band (symmetric
    with the read side; ms-161 AX review PR#694). Raises ``DeliverableValidationError``
    if ``entry_id`` is not in the log — retiring a non-existent entry is a caller
    bug, surfaced loudly rather than a silent no-op. Idempotent-safe: retiring an
    already-retired entry simply re-stamps."""
    entry = _find_entry(data, entry_id)
    if entry is None:
        raise DeliverableValidationError(
            f"cannot retire unknown deliverable {entry_id!r}")
    _stamp_transition(entry, STATUS_RETIRED, at, actor)
    reason = (reason or "").strip()
    if reason:
        entry["retire_reason"] = reason
    return _copy_entry(entry)


def supersede_deliverable(data: dict, old_id: str, new_entry: dict, *,
                          at: str | None = None,
                          actor: str | None = None) -> dict:
    """Replace an earlier produced-value entry with an evolved one — the "足す＆
    消す" done atomically (方針3): APPEND ``new_entry`` (forcing its ``supersedes``
    pointer to ``old_id``) AND flip ``old_id`` to ``superseded`` so only the
    successor remains in the current-state set.

    ``new_entry`` is validated/appended through ``append_deliverable`` (so a bad
    successor raises BEFORE the old entry is touched — the transition is
    all-or-nothing). Its ``supersedes`` is SET to ``old_id`` by this operation
    regardless of any value the caller put there: the link is defined by the
    operation, not hand-authored. Returns a COPY of the stored successor entry (via
    ``append_deliverable``), so mutating the result cannot corrupt the log —
    symmetric with append/retire/read (ms-161 AX review PR#694). Raises if
    ``old_id`` is absent (cannot supersede what is not there)."""
    old = _find_entry(data, old_id)
    if old is None:
        raise DeliverableValidationError(
            f"cannot supersede unknown deliverable {old_id!r}")
    # The successor's supersedes link is owned by THIS operation (override any
    # caller value) so the pointer and the flip below can never disagree.
    successor = append_deliverable(data, {**new_entry, "supersedes": old_id},
                                   at=at, actor=actor)
    _stamp_transition(old, STATUS_SUPERSEDED, at, actor)
    old["superseded_by"] = successor["id"]
    return successor


def active_deliverables(data: dict) -> list:
    """The current-state view — the entries a summary/map treats as WHAT THE
    PROJECT CAN DO NOW (方針3: active のみ要約 = 現在地). This is the input the map
    projector (e-5824) groups by category.

    An entry is current iff BOTH hold:

    1. its own ``status`` is ``active`` (not retired / superseded), AND
    2. no OTHER active entry supersedes it — a defensive derivation so a live
       successor removes its predecessor even if the predecessor's status was
       never flipped (e.g. a plain ``append_deliverable`` that set ``supersedes``
       without going through ``supersede_deliverable``). Belt-and-suspenders for
       the "自動脱落" guarantee: the summary never double-counts an evolved
       capability.

    Returns fresh copies (non-mutating for consumers), insertion order preserved."""
    entries = [e for e in _log(data) if isinstance(e, dict)]
    active = [e for e in entries if e.get("status") == STATUS_ACTIVE]
    # ids that a still-active entry claims to supersede — their predecessors are
    # no longer current even if not explicitly flipped.
    superseded_by_live = {
        e.get("supersedes") for e in active if e.get("supersedes")}
    return [_copy_entry(e) for e in active
            if e.get("id") not in superseded_by_live]
