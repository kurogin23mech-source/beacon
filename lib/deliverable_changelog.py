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
            "source is required and must be a {target_id, kind} object")
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
    append) and returns the stored dict. The CALLER owns persistence — mirroring
    ``root_target.set_root_label`` — so this module stays I/O-free and the
    cloud/local save path is unchanged."""
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
    return stamped


def read_deliverables(data: dict, *,
                      status: str | None = None,
                      category: str | None = None,
                      source_target: str | None = None) -> list:
    """Read the root's deliverable-changelog, newest-append last (insertion
    order), with optional filters (受入条件1 read side).

    - ``status`` — keep only entries with this lifecycle status (e.g.
      ``STATUS_ACTIVE`` for the current-state view the map projector will use;
      the active-only SUMMARY logic itself is e-5824). An unknown status value
      raises, symmetric with append-time validation, so a filter typo is loud.
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
        copy = dict(entry)
        if isinstance(entry.get("source"), dict):
            copy["source"] = dict(entry["source"])
        if isinstance(entry.get("tags"), list):
            copy["tags"] = list(entry["tags"])
        out.append(copy)
    return out
