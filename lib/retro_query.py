"""Unified retro / retrospect query base (ms-79 / e-1836).

Single source of truth for the search / aggregation logic shared by:

  - ``cmd_retro_prepare``  (= /beacon-retro Skill, week-window aggregation)
  - ``cmd_search``          (= /beacon-retrospect Skill, arbitrary query)

Why this module exists (= the "入り口 2 つ・内部 1 つ" goal):

  Before ms-79 / e-1836, the two entry points each implemented their own
  walker — ``core.collect_retro_entries`` for retro (= recursive date-range
  walk of ms entries) and ``search.search_project`` for retrospect (=
  fulltext + facet search across all entity types). Improvements to one
  side did not flow to the other; the UC10 finding cluster
  (e-1832 / e-1833 / e-1834 / e-1835) would have had to be implemented
  twice (once in core, once in search) to actually surface in both Skills.

  This module adds the **共通基盤** layer: a thin wrapper around
  ``search.search_project`` that the retro side now uses, plus the
  extension filters (source / actor / claim / bus-DM / fork session log /
  Trek summary) that UC10 calls for. Both Skills consume the same query
  surface, so a future UC-finding only needs to be implemented once.

Surface (per SPEC ms-79 §3 ‘設計の柱 6’ — 基盤統合):

  retro_query(
      project,
      documents=None,
      *,
      q="",
      type=None, status=None, priority=None, scope="",
      ms="", op="", id="",
      assignee="", owner="",
      from_date="", to_date="",
      # ms-79 extensions:
      source=None,       # e-1833 / UC10-F2: ["human" | "auto-op" | "dm"]
      actor="",          # e-1834 / UC10-F3: actor.agent or actor.machine match
      claimant="",       # e-1834 / UC10-F3: claim assignee
      include_bus_dm=False,  # e-1832 / UC10-F1: pull bus archive entries
      include_session_logs=False,  # e-1835 / UC10-F4: include fork session log
      include_trek=False,          # e-1835 / UC10-F4: include Trek summaries
      limit=50, offset=0,
  ) -> dict

  Returns the same shape as ``search.search_project`` (results / total /
  limit / offset / facets), augmented with the extension entity types
  when their include flags are set.

Design constraints:

  - Pure-ish function. I/O is limited to what the caller passes in
    (project dict, documents list, session_logs list, bus archive list).
    The CLI layer (commands.py) is responsible for fetching those from
    storage; we accept them as arguments so unit tests can drive the
    module directly.
  - Backwards compatible: every existing keyword that ``search_project``
    accepts is passed through unchanged. Old callers (server-side
    /api/projects/{id}/search) keep working without modification.
  - No new "search dialect". Extensions are post-filters on the
    canonical search result list, so the contract documented in
    SPEC ``3ne57ccZegYQXDQA03op`` (Beacon 検索基盤の原則) is preserved.

Note on retro week-window aggregation:

  ``cmd_retro_prepare`` does more than search — it also groups results
  by milestone and computes per-MS deltas (progress, completed tasks,
  etc.). That **per-MS rollup** is retro-specific and stays in
  ``commands.py``. This module is the search / filter / source-merge
  layer; the rollup layer above can keep iterating without needing
  another refactor.
"""

from __future__ import annotations

from typing import Any, Optional

import search as _search


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def retro_query(
    project: dict,
    documents: Optional[list[dict]] = None,
    *,
    # — passthrough to search.search_project —
    q: str = "",
    type: Optional[list[str]] = None,
    status: Optional[list[str]] = None,
    priority: Optional[list[str]] = None,
    scope: str = "",
    ms: str = "",
    op: str = "",
    id: str = "",
    assignee: str = "",
    owner: str = "",
    from_date: str = "",
    to_date: str = "",
    limit: int = 50,
    offset: int = 0,
    # — ms-79 extensions —
    source: Optional[list[str]] = None,
    actor: str = "",
    claimant: str = "",
    include_bus_dm: bool = False,
    include_session_logs: bool = False,
    include_trek: bool = False,
    # — optional pre-fetched extension data (kept I/O-free) —
    session_logs: Optional[list[dict]] = None,
    bus_archive: Optional[list[dict]] = None,
    trek_summaries: Optional[list[dict]] = None,
) -> dict:
    """Run the unified retro / retrospect query.

    Returns the standard search result shape::

        {
          "results": [...],
          "total": N,
          "limit": L,
          "offset": O,
          "facets": {type: {...}, status: {...}, priority: {...}, scope: {...}},
        }

    Plus, when extension flags are set, the result list may include
    entities whose ``entity_type`` is ``"session_log"`` / ``"bus_event"``
    / ``"trek_summary"``. These types pass through facet building so
    callers can render them generically.

    The ``source`` / ``actor`` / ``claimant`` filters are applied as
    post-filters on the canonical search result. They do NOT alter
    search_project's own contract (it still ignores those fields).
    Doing the filter here keeps the module addition non-invasive — the
    UC3-F3 ``meta.source`` field (e-1817) need only be persisted by
    log_commit; this module knows how to read it.
    """
    # ---- 1. canonical search via search.search_project (unchanged) ----
    base = _search.search_project(
        project,
        documents,
        q=q,
        type=type,
        status=status,
        priority=priority,
        scope=scope,
        ms=ms,
        op=op,
        id=id,
        assignee=assignee,
        owner=owner,
        from_date=from_date,
        to_date=to_date,
        # pull a wide window — we post-filter and re-paginate below.
        limit=10_000,
        offset=0,
    )
    results: list[dict] = base["results"]

    # ---- 2. ms-79 extensions: pull in source-side rows from project ----
    # We re-walk the project dict for the meta fields the rendered search
    # results don't carry (meta.source / meta.actor / meta.session_id /
    # claim assignee). Cheap: the project tree is already in memory.
    meta_index = _build_meta_index(project)

    # ---- 3. extension entity merge (= bus / session_log / Trek) ----
    if include_session_logs and session_logs:
        for sl in session_logs:
            results.append(_render_session_log(sl, q))

    if include_bus_dm and bus_archive:
        for ev in bus_archive:
            rendered = _render_bus_event(ev, q)
            if rendered:
                results.append(rendered)

    if include_trek and trek_summaries:
        for ts in trek_summaries:
            results.append(_render_trek_summary(ts, q))

    # ---- 4. ms-79 post-filters (source / actor / claimant) ----
    if source:
        source_set = set(source)
        # Source filter only applies to "history event" rows (commit / task /
        # save / pr / push / deploy + extension types). Containers (milestone
        # / operation / document) are filtered out entirely — they aren't
        # source-tagged events. Without this, a "show me only human commits"
        # query would still surface every milestone row as "human" (their
        # default), drowning out the actual commit results.
        results = [r for r in results
                   if _is_source_taggable(r)
                   and _entry_source(r, meta_index) in source_set]

    if actor:
        results = [r for r in results
                   if _entry_actor(r, meta_index) == actor
                   or _entry_actor_machine(r, meta_index) == actor]

    if claimant:
        results = [r for r in results
                   if _entry_claimant(r, meta_index, project) == claimant]

    # ---- 5. sort + paginate (same order as search.search_project) ----
    results.sort(key=lambda r: r.get("updated_at") or r.get("created_at") or "",
                 reverse=True)
    total = len(results)
    paged = results[offset:offset + limit]

    # ---- 6. rebuild facets from filtered set (= reflect post-filters) ----
    facets = _build_facets(results)
    # Extension facet: source counts (= UC10-F2 "human N / auto-op M / dm K").
    facets["source"] = _build_source_facet(results, meta_index)

    return {
        "results": paged,
        "total": total,
        "limit": limit,
        "offset": offset,
        "facets": facets,
    }


# ---------------------------------------------------------------------------
# Meta index — pulls source / actor / claim from the raw project dict
# ---------------------------------------------------------------------------

def _build_meta_index(project: dict) -> dict[str, dict]:
    """Map entry id → meta dict so post-filters can read fields the
    search renderer does not expose.

    The renderer in lib/search.py keeps only display fields. For UC3 /
    UC10 post-filters we need meta.source, meta.actor, meta.session_id,
    meta.envelope_id. Walking the project once and indexing by id is
    much cheaper than re-finding each entry per filter call.
    """
    out: dict[str, dict] = {}
    for ms in project.get("milestones", []) or []:
        _index_entries(ms.get("entries", []) or [], out)
    for op in project.get("operations", []) or []:
        _index_entries(op.get("entries", []) or [], out)
    return out


def _index_entries(entries: list, out: dict) -> None:
    for e in entries:
        eid = e.get("id") or ""
        if eid:
            out[eid] = e.get("meta") or {}
        nested = e.get("entries") or []
        if nested:
            _index_entries(nested, out)


# entity types that carry a meaningful per-event source label. Milestones,
# operations and documents are containers / artifacts — they don't have a
# "human vs auto-op" axis. Keep this list tight; adding a type means the
# source filter will start filtering it.
_SOURCE_TAGGABLE_TYPES = {
    "commit", "task", "save", "pr", "push", "deploy",
    "operation_task", "run", "incident",
    "session_log", "bus_event", "trek_summary",
}


def _is_source_taggable(result: dict) -> bool:
    return (result.get("entity_type") or "") in _SOURCE_TAGGABLE_TYPES


def _entry_source(result: dict, meta_index: dict) -> str:
    """Return the source label for a search result row.

    Resolution order:
      1. meta.source on the underlying entry (set by log_commit when an
         envelope context is present — e-1817 / UC3-F3).
      2. entity_type-based default:
         - session_log / bus_event / trek_summary → their own label
         - otherwise → "human" (= the historical default for entries
           without a source tag).
    """
    et = result.get("entity_type") or ""
    if et == "session_log":
        return "session_log"
    if et == "bus_event":
        return "dm"
    if et == "trek_summary":
        return "trek"

    meta = meta_index.get(result.get("id") or "") or {}
    src = meta.get("source")
    if isinstance(src, str) and src:
        return src
    return "human"


def _entry_actor(result: dict, meta_index: dict) -> str:
    """Return the actor.agent string for a result, or "".

    Reads ``meta.actor.agent`` written by ``core.log_commit``
    (= ms-51 / e-934, the multi-machine / multi-agent identifier).
    """
    meta = meta_index.get(result.get("id") or "") or {}
    actor = meta.get("actor") or {}
    if isinstance(actor, dict):
        return actor.get("agent", "") or ""
    return ""


def _entry_actor_machine(result: dict, meta_index: dict) -> str:
    """Return the actor.machine string for a result, or ""."""
    meta = meta_index.get(result.get("id") or "") or {}
    actor = meta.get("actor") or {}
    if isinstance(actor, dict):
        return actor.get("machine", "") or ""
    return ""


def _entry_claimant(result: dict, meta_index: dict, project: dict) -> str:
    """Return the claimant for a result, or "".

    Claims are dynamic work declarations (= ms-55) on a milestone. A
    result belongs to a claimant if:
      - it is a ms (own claim_holder field), OR
      - it is an entry under a ms whose claim_holder matches.
    For now we only check ``meta.claim_holder`` if the entry was
    recorded under a claim, plus the milestone-level claim_holder.
    """
    meta = meta_index.get(result.get("id") or "") or {}
    holder = meta.get("claim_holder")
    if isinstance(holder, str) and holder:
        return holder
    ms_id = result.get("ms_id") or ""
    if ms_id:
        for ms in project.get("milestones", []) or []:
            if ms.get("id") == ms_id:
                ch = ms.get("claim_holder")
                if isinstance(ch, str):
                    return ch
                break
    return ""


# ---------------------------------------------------------------------------
# Extension entity renderers (bus / session_log / trek)
# ---------------------------------------------------------------------------

def _render_session_log(sl: dict, q: str) -> dict:
    """Render a session_log row into the standard search result shape.

    session_log records carry per-session aggregated summaries (e-1037)
    plus the fork attribution that e-1835 (UC10-F4) wants surfaced.
    """
    summary = sl.get("summary") or ""
    snippet = _snippet(summary, q.lower() if q else "")
    sid = sl.get("session_id") or sl.get("id") or ""
    return {
        "entity_type": "session_log",
        "id": sid,
        "title": f"session {sid[:12]}",
        "snippet": snippet,
        "ms_id": sl.get("ms_id"),
        "op_id": None,
        "status": None,
        "priority": None,
        "scope": None,
        "created_at": sl.get("created_at", ""),
        "updated_at": sl.get("last_aggregated_at") or sl.get("created_at", ""),
        "url_hash": f"#session-log/{sid}",
        # extra marker for the Skill output ("fork 由来" / "Trek 由来" 表示)
        "from_fork": bool(sl.get("fork_parent_session_id")),
    }


def _render_bus_event(ev: dict, q: str) -> Optional[dict]:
    """Render a bus / DM archive event into the standard result shape.

    Returns None if the event has no human-readable payload (= skip
    heartbeat-only entries).
    """
    payload = ev.get("payload") or ev.get("text") or ev.get("body") or ""
    if not payload and not ev.get("envelope_id"):
        return None
    snippet = _snippet(payload, q.lower() if q else "")
    eid = ev.get("event_id") or ev.get("envelope_id") or ev.get("id") or ""
    return {
        "entity_type": "bus_event",
        "id": eid,
        "title": f"DM {ev.get('channel', '')} {eid[:12]}".strip(),
        "snippet": snippet,
        "ms_id": None,
        "op_id": None,
        "status": None,
        "priority": None,
        "scope": None,
        "created_at": ev.get("created_at", ""),
        "updated_at": ev.get("created_at", ""),
        "url_hash": f"#bus/{eid}",
        # marker so retrospect output can label "DM 由来"
        "from_dm": True,
    }


def _render_trek_summary(ts: dict, q: str) -> dict:
    """Render a Trek summary (= /beacon-trek summary-add 経由) into a row."""
    text = ts.get("text") or ts.get("summary") or ""
    snippet = _snippet(text, q.lower() if q else "")
    tid = ts.get("id") or ""
    trek_id = ts.get("trek_id") or ""
    return {
        "entity_type": "trek_summary",
        "id": tid,
        "title": f"Trek {trek_id} summary",
        "snippet": snippet,
        "ms_id": None,
        "op_id": None,
        "status": None,
        "priority": None,
        "scope": None,
        "created_at": ts.get("created_at", ""),
        "updated_at": ts.get("created_at", ""),
        "url_hash": f"#trek/{trek_id}/{tid}",
        # marker so retrospect can label "Trek 由来"
        "from_trek": True,
    }


# ---------------------------------------------------------------------------
# Facets
# ---------------------------------------------------------------------------

def _build_facets(matches: list[dict]) -> dict:
    facets: dict[str, dict[str, int]] = {
        "type": {}, "status": {}, "priority": {}, "scope": {},
    }
    for m in matches:
        t = m.get("entity_type") or "?"
        facets["type"][t] = facets["type"].get(t, 0) + 1
        s = m.get("status")
        if s:
            facets["status"][s] = facets["status"].get(s, 0) + 1
        p = m.get("priority")
        if p:
            facets["priority"][p] = facets["priority"].get(p, 0) + 1
        sc = m.get("scope")
        if sc:
            facets["scope"][sc] = facets["scope"].get(sc, 0) + 1
    return facets


def _build_source_facet(matches: list[dict], meta_index: dict) -> dict[str, int]:
    """Count results by source label (human / auto-op / dm / trek / session_log).

    Surfaced in retrospect output for the UC10-F2 ‘該当 N 件: human 5 件 /
    auto-op 3 件 / DM 2 件’ summary.
    """
    out: dict[str, int] = {}
    for r in matches:
        # Only count rows that have a meaningful source axis (= match what
        # the source filter would keep). Otherwise milestone / operation
        # / document containers would inflate the "human" bucket.
        if not _is_source_taggable(r):
            continue
        s = _entry_source(r, meta_index)
        out[s] = out.get(s, 0) + 1
    return out


# ---------------------------------------------------------------------------
# Snippet helper (mirrors lib/search._snippet to avoid cross-module pull)
# ---------------------------------------------------------------------------

def _snippet(text: str, q_lower: str, *, radius: int = 80) -> str:
    if not text:
        return ""
    text = text.strip()
    if not q_lower:
        return text[:200].replace("\n", " ")
    text_lower = text.lower()
    idx = text_lower.find(q_lower)
    if idx < 0:
        return text[:200].replace("\n", " ")
    start = max(0, idx - radius)
    end = min(len(text), idx + len(q_lower) + radius)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return (prefix + text[start:end] + suffix).replace("\n", " ")


__all__ = ["retro_query"]
