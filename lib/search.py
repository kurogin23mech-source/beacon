"""Unified search across all Beacon entities (CORE doc `Beacon 検索基盤の原則`).

Single source of truth for search logic; called by:
  - cmd_search (CLI local mode)
  - /api/projects/{id}/search (server)
  - /beacon-retrospect Skill (via the CLI / API)

Design (matches SPEC doc 3ne57ccZegYQXDQA03op):
  - Pure function: project dict + documents list + params → results + facets
  - No I/O inside (storage caller fetches data, search returns results)
  - Phase 1: server-side scan (no index). Phase 2 path is SQLite FTS5,
    but the API contract is unchanged so callers are unaffected.

Searchable entities and their `q` fields:

  milestone:       title, objective, acceptance_criteria, description
  task:            description, detail, motivation, acceptance_criteria, behavior
  commit:          description, meta.message, meta.hash
  save:            description, meta (JSON-stringified)
  pr:              description, meta.intent, meta.review_rationale, title
  document:        title, content
  push:            description, summary
  deploy:          description, summary
  operation:       title, activation_hint, description
  operation_task:  description, detail
  run:             description, batch
  incident:        title, description, resolution

  Generic Target classes (ms-109 e-5524) — a sales project's opportunity /
  account, a dev project's release, or any data-defined occupation's class —
  are enumerated through the target-class engine and matched on the fallback
  fields (label, name, title, description). Only development entry types keep a
  bespoke q-field list above; the fallback (`_Q_FIELDS_FALLBACK`) is the single
  source for every other class, so declaring a new class needs no edit here.

Every entity's own id (e-XXX / ms-XX / op-X) and a document's doc_id are also
matched by `q` as a substring, so users can find an entry by number — e.g.
"e-1005" or a partial "1005" (ms-43 e-1010).

Metadata filters (specified separately from `q`, not part of fulltext):
  type[], status[], priority[], scope, ms, op, id (exact),
  assignee, owner, from (created_at ≥), to (created_at ≤),
  limit, offset
"""

from __future__ import annotations

import json
from typing import Any, Optional

import occupation
import work_model


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def search_project(
    project: dict,
    documents: Optional[list[dict]] = None,
    *,
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
) -> dict:
    """Return paginated search results across all Beacon entities.

    All filters are AND-combined. Within each filter list (type, status,
    priority), values are OR-combined.

    Returns:
      {
        "results": [{entity_type, id, title, snippet, ms_id, op_id,
                     status, priority, created_at, url_hash, ...}],
        "total": N,         # total matched (pre-pagination)
        "limit": L,
        "offset": O,
        "facets": {type: {...}, status: {...}, priority: {...}},
      }
    """
    documents = documents or []
    type_filter = set(type) if type else None
    status_filter = set(status) if status else None
    priority_filter = set(priority) if priority else None
    q_lower = q.lower().strip() if q else ""

    matches: list[dict] = []

    # ------------------ Milestones ------------------
    for m in project.get("milestones", []):
        # ms_filter narrows the entire milestone scope — skip non-target MSs
        # (their entries inherit MS scope by location, not by an ms_id field).
        if ms and m.get("id") != ms:
            continue
        if _entity_matches(
            "milestone", m,
            q_lower=q_lower,
            type_filter=type_filter,
            status_filter=status_filter,
            priority_filter=priority_filter,
            ms_filter=ms,
            op_filter=op,
            id_filter=id,
            assignee=assignee,
            owner=owner,
            from_date=from_date,
            to_date=to_date,
            scope_filter=None,
        ):
            matches.append(_render_milestone(m, q_lower))

        # walk into milestone entries
        for ent in m.get("entries", []):
            _walk_entry(ent, m, matches, q_lower=q_lower,
                        type_filter=type_filter,
                        status_filter=status_filter,
                        priority_filter=priority_filter,
                        ms_filter=ms,
                        op_filter=op,
                        id_filter=id,
                        assignee=assignee,
                        owner=owner,
                        from_date=from_date,
                        to_date=to_date)

    # ------------------ Operations + nested ------------------
    for opn in project.get("operations", []):
        # op_filter narrows the entire Operation scope (analogous to ms_filter)
        if op and opn.get("id") != op:
            continue
        if _entity_matches(
            "operation", opn,
            q_lower=q_lower,
            type_filter=type_filter,
            status_filter=status_filter,
            priority_filter=priority_filter,
            ms_filter=ms,
            op_filter=op,
            id_filter=id,
            assignee=assignee,
            owner=owner,
            from_date=from_date,
            to_date=to_date,
            scope_filter=None,
        ):
            matches.append(_render_operation(opn, q_lower))

        for ent in opn.get("entries", []):
            _walk_entry(ent, opn, matches, q_lower=q_lower,
                        type_filter=type_filter,
                        status_filter=status_filter,
                        priority_filter=priority_filter,
                        ms_filter=ms,
                        op_filter=op,
                        id_filter=id,
                        assignee=assignee,
                        owner=owner,
                        from_date=from_date,
                        to_date=to_date,
                        parent_is_op=True)

    # ------------------ Other Target classes (generic engine) ------------------
    # ms-109 e-5524 (C5 per-class strangler): the milestone / operation loops above
    # are the two Target classes with dedicated renderers (_DEDICATED_TARGET_RENDERERS).
    # Every OTHER manifest Target class — a sales project's opportunities / accounts,
    # a dev project's release Targets, or any data-defined occupation's classes — is
    # enumerated here through the generic target-class engine
    # (occupation.profession_manifest) instead of a hardcoded collection key, and
    # rendered by the one occupation-agnostic renderer. Before this, only dev
    # milestones/operations were searchable; a sales project's Targets (and dev
    # releases) were silently unfindable.
    #
    # ms / op are development-scope filters (a Target with no ms_id/op_id would
    # otherwise leak into an --ms/--op query, since _entity_matches passes records
    # that carry no such field). These generic classes have no ms/op scope, so the
    # whole loop is skipped when either filter is set — filtering by a dev scope
    # asks for that scope's dev entries only (AX review PR#683).
    #
    # Scope note: this walks the Target records themselves. Their fat arms
    # (a sales Target's activities / communications / nurturings) are a follow-up
    # (e-5524 remainder).
    if not ms and not op:
        for _tc in occupation.profession_manifest(project).get("target_classes", []):
            _kind = _tc.get("kind") or ""
            _coll = _tc.get("collection") or ""
            if not _kind or not _coll or _kind in _DEDICATED_TARGET_RENDERERS:
                continue
            # The class's declared state field (dev = "status", a funnel class =
            # "phase") drives BOTH the status filter and the rendered status, so
            # they agree — the manifest stays the single truth (AX review PR#683).
            _state_field = ((_tc.get("state_model") or {}).get("state_field")) or "status"
            for tgt in project.get(_coll, []) or []:
                if _entity_matches(
                    _kind, tgt,
                    q_lower=q_lower,
                    type_filter=type_filter,
                    status_filter=status_filter,
                    priority_filter=priority_filter,
                    ms_filter=ms,
                    op_filter=op,
                    id_filter=id,
                    assignee=assignee,
                    owner=owner,
                    from_date=from_date,
                    to_date=to_date,
                    scope_filter=None,
                    state_field=_state_field,
                ):
                    matches.append(_render_target_generic(tgt, _kind, q_lower,
                                                           state_field=_state_field))

    # ------------------ Pushes / Deploys ------------------
    for push in project.get("pushes", []):
        # Treat push entries as if type=push
        synth = {**push, "type": "push",
                 "id": push.get("id", ""),
                 "description": push.get("summary", "") or push.get("description", ""),
                 "created_at": push.get("pushed_at", "")}
        if _entity_matches(
            "push", synth,
            q_lower=q_lower,
            type_filter=type_filter,
            status_filter=status_filter,
            priority_filter=priority_filter,
            ms_filter=ms,
            op_filter="",
            id_filter=id,
            assignee=assignee,
            owner=owner,
            from_date=from_date,
            to_date=to_date,
            scope_filter=None,
        ):
            if not ms or push.get("ms_id") == ms:
                matches.append(_render_push(synth, q_lower))

    for dep in project.get("deployments", []) or project.get("deploys", []):
        synth = {**dep, "type": "deploy",
                 "id": dep.get("id", ""),
                 "description": dep.get("description", "") or dep.get("desc", ""),
                 "created_at": dep.get("deployed_at", "") or dep.get("date", "")}
        if _entity_matches(
            "deploy", synth,
            q_lower=q_lower,
            type_filter=type_filter,
            status_filter=status_filter,
            priority_filter=priority_filter,
            ms_filter=ms,
            op_filter="",
            id_filter=id,
            assignee=assignee,
            owner=owner,
            from_date=from_date,
            to_date=to_date,
            scope_filter=None,
        ):
            matches.append(_render_deploy(synth, q_lower))

    # ------------------ Documents (separate collection) ------------------
    for doc in documents:
        if _document_matches(doc, q_lower=q_lower,
                              type_filter=type_filter,
                              scope_filter=scope,
                              ms_filter=ms,
                              op_filter=op,
                              id_filter=id,
                              from_date=from_date,
                              to_date=to_date):
            matches.append(_render_document(doc, q_lower))

    # ------------------ Facets (from matched set, pre-pagination) ------------------
    facets = _build_facets(matches)

    # ------------------ Sort: most recent first ------------------
    matches.sort(key=lambda r: r.get("updated_at") or r.get("created_at") or "", reverse=True)

    total = len(matches)

    # ------------------ Paginate ------------------
    paged = matches[offset:offset + limit]

    return {
        "results": paged,
        "total": total,
        "limit": limit,
        "offset": offset,
        "facets": facets,
    }


# ---------------------------------------------------------------------------
# Recursive entry walker (handles nested entries inside milestones/operations)
# ---------------------------------------------------------------------------

def _walk_entry(entry: dict, parent: dict, matches: list[dict], *,
                q_lower: str, type_filter, status_filter, priority_filter,
                ms_filter, op_filter, id_filter, assignee, owner,
                from_date, to_date, parent_is_op: bool = False):
    entry_type = entry.get("type", "")

    # Map nested entry types to their search-facing names if needed.
    if parent_is_op and entry_type == "task":
        # Operation-attached "task" entries are operation_tasks (準備項目)
        effective_type = "operation_task"
    else:
        effective_type = entry_type

    if _entity_matches(
        effective_type, entry,
        q_lower=q_lower,
        type_filter=type_filter,
        status_filter=status_filter,
        priority_filter=priority_filter,
        ms_filter=ms_filter,
        op_filter=op_filter,
        id_filter=id_filter,
        assignee=assignee,
        owner=owner,
        from_date=from_date,
        to_date=to_date,
        scope_filter=None,
    ):
        # Only attach if scope checks pass
        parent_id = parent.get("id", "")
        parent_title = parent.get("title", "")
        if parent_is_op:
            matches.append(_render_entry(entry, effective_type, q_lower,
                                          op_id=parent_id, op_title=parent_title))
        else:
            matches.append(_render_entry(entry, effective_type, q_lower,
                                          ms_id=parent_id, ms_title=parent_title))

    for child in entry.get("entries", []) or []:
        _walk_entry(child, parent, matches, q_lower=q_lower,
                    type_filter=type_filter,
                    status_filter=status_filter,
                    priority_filter=priority_filter,
                    ms_filter=ms_filter,
                    op_filter=op_filter,
                    id_filter=id_filter,
                    assignee=assignee,
                    owner=owner,
                    from_date=from_date,
                    to_date=to_date,
                    parent_is_op=parent_is_op)


# ---------------------------------------------------------------------------
# Entity match check (shared for milestones / entries / operations)
# ---------------------------------------------------------------------------

_Q_FIELDS_BY_TYPE = {
    "milestone":      ["title", "objective", "acceptance_criteria", "description"],
    "task":           ["description", "detail", "motivation", "acceptance_criteria", "behavior"],
    "commit":         ["description", ("meta", "message"), ("meta", "hash")],
    "save":           ["description", ("meta_str",)],
    "pr":             ["description", "title", ("meta", "intent"), ("meta", "review_rationale")],
    "push":           ["description", "summary"],
    "deploy":         ["description", "summary"],
    "operation":      ["title", "activation_hint", "description"],
    "operation_task": ["description", "detail"],
    "run":            ["description", "batch"],
    "incident":       ["title", "description", "resolution"],
}

# Fulltext fields for any entity type not listed above — notably every generic
# Target class (sales opportunities/accounts, dev releases, any data-defined
# class). It leads with ``label`` (the canonical display-name key work_model.
# target_label reads) so a Target is findable by the name shown in its result,
# with ``name``/``title`` legacy fallbacks (ms-109 e-5524). Keyed per-kind entries
# stay reserved for the development entry types whose q-fields genuinely differ.
_Q_FIELDS_FALLBACK = ["label", "name", "title", "description"]


def _entity_matches(entity_type: str, e: dict, *, q_lower: str,
                    type_filter, status_filter, priority_filter,
                    ms_filter: str, op_filter: str, id_filter: str,
                    assignee: str, owner: str,
                    from_date: str, to_date: str,
                    scope_filter, state_field: str = "status") -> bool:
    # type filter
    if type_filter is not None and entity_type not in type_filter:
        return False

    # id filter (exact match)
    if id_filter and e.get("id") != id_filter:
        return False

    # status filter — read the class's declared state field (dev = "status",
    # a phase-driven sales class = "phase"), so filtering by a value the result
    # displays as status returns the same set (ms-109 e-5524 AX round-trip).
    if status_filter is not None:
        if e.get(state_field, "") not in status_filter:
            return False

    # priority filter
    if priority_filter is not None:
        meta = e.get("meta") or {}
        priority = e.get("priority") or meta.get("priority", "")
        if priority not in priority_filter:
            return False

    # ms filter (applies to milestone/task/commit/save/pr — handled by caller
    # context already, but enforce on entry as well via ms_id field if present)
    if ms_filter:
        # If entity is the milestone itself
        if entity_type == "milestone":
            if e.get("id") != ms_filter:
                return False
        else:
            ms_id_value = e.get("ms_id") or (e.get("meta") or {}).get("ms_id", "")
            # Don't enforce on entries that don't carry ms_id explicitly;
            # the caller's iteration scope already restricted to the right MS.
            if ms_id_value and ms_id_value != ms_filter:
                return False

    # op filter
    if op_filter:
        if entity_type == "operation":
            if e.get("id") != op_filter:
                return False
        # Otherwise: entries inside an operation are handled by caller scope

    # assignee / owner
    if assignee:
        if e.get("assignee", "") != assignee and (e.get("meta") or {}).get("assignee", "") != assignee:
            return False
    if owner:
        if e.get("owner", "") != owner and (e.get("meta") or {}).get("owner", "") != owner:
            return False

    # date range
    created_at = e.get("created_at", "") or e.get("date", "") or e.get("pushed_at", "")
    if from_date and created_at and created_at < from_date:
        return False
    if to_date and created_at and created_at > to_date:
        return False

    # fulltext q — also match the entity's own id (e-XXX / ms-XX / op-X) so a
    # query like "e-1005" or a partial "1005" finds the entry by number. Users
    # routinely refer to entries by id ("what was e-1005 again?") (ms-43 e-1010).
    if q_lower:
        eid = (e.get("id") or "").lower()
        if q_lower not in eid and not _matches_fulltext(entity_type, e, q_lower):
            return False

    return True


def _matches_fulltext(entity_type: str, e: dict, q_lower: str) -> bool:
    fields = _Q_FIELDS_BY_TYPE.get(entity_type, _Q_FIELDS_FALLBACK)
    for f in fields:
        if isinstance(f, tuple):
            if f[0] == "meta_str":
                val = json.dumps(e.get("meta") or {}, ensure_ascii=False)
            else:
                container = e
                for k in f:
                    container = (container or {}).get(k) if isinstance(container, dict) else None
                val = container or ""
        else:
            val = e.get(f, "") or ""
        if isinstance(val, str) and q_lower in val.lower():
            return True
    return False


def _document_matches(doc: dict, *, q_lower: str, type_filter,
                       scope_filter: str, ms_filter: str, op_filter: str,
                       id_filter: str, from_date: str, to_date: str) -> bool:
    if type_filter is not None and "document" not in type_filter:
        return False
    if scope_filter and doc.get("scope", "") != scope_filter:
        return False
    if ms_filter and doc.get("milestone", "") != ms_filter:
        return False
    if op_filter and doc.get("operation", "") != op_filter:
        return False
    if id_filter and doc.get("doc_id", "") != id_filter:
        return False
    updated = doc.get("updated_at", "") or doc.get("created_at", "")
    if from_date and updated and updated < from_date:
        return False
    if to_date and updated and updated > to_date:
        return False
    if q_lower:
        did = (doc.get("doc_id") or doc.get("id") or "").lower()
        title = (doc.get("title") or "").lower()
        content = (doc.get("content") or "").lower()
        if q_lower not in did and q_lower not in title and q_lower not in content:
            return False
    return True


# ---------------------------------------------------------------------------
# Result rendering (uniform shape for caller)
# ---------------------------------------------------------------------------

def _render_milestone(m: dict, q_lower: str) -> dict:
    return {
        "entity_type": "milestone",
        "id": m.get("id", ""),
        "title": m.get("title", ""),
        "snippet": _snippet(m.get("description") or m.get("objective") or "", q_lower),
        "ms_id": m.get("id", ""),
        "ms_title": m.get("title", ""),
        "op_id": None,
        "status": m.get("status", ""),
        "priority": m.get("priority", ""),
        "assignee": m.get("assignee", ""),
        "owner": m.get("owner", ""),
        "scope": None,
        "created_at": m.get("created_at", ""),
        "updated_at": m.get("updated_at", "") or m.get("created_at", ""),
        "url_hash": f"#ms-{m.get('id', '').replace('ms-', '')}" if m.get("id") else "",
    }


def _render_operation(opn: dict, q_lower: str) -> dict:
    return {
        "entity_type": "operation",
        "id": opn.get("id", ""),
        "title": opn.get("title", ""),
        "snippet": _snippet(opn.get("activation_hint") or opn.get("description") or "", q_lower),
        "ms_id": None,
        "op_id": opn.get("id", ""),
        "op_title": opn.get("title", ""),
        "status": opn.get("status", ""),
        "priority": opn.get("priority", ""),
        "created_at": opn.get("created_at", ""),
        "updated_at": opn.get("updated_at", "") or opn.get("created_at", ""),
        "url_hash": f"#operations/{opn.get('id', '')}" if opn.get("id") else "",
    }


def _render_entry(entry: dict, effective_type: str, q_lower: str, *,
                   ms_id: str = "", ms_title: str = "",
                   op_id: str = "", op_title: str = "") -> dict:
    meta = entry.get("meta") or {}
    src = entry.get("description", "") or entry.get("title", "")
    return {
        "entity_type": effective_type,
        "id": entry.get("id", ""),
        "title": entry.get("title", "") or _first_line(src),
        "snippet": _snippet(src, q_lower),
        "ms_id": ms_id or meta.get("ms_id") or "",
        "ms_title": ms_title,
        "op_id": op_id or meta.get("op_id") or "",
        "op_title": op_title,
        "status": entry.get("status", ""),
        "priority": entry.get("priority", "") or meta.get("priority", ""),
        "assignee": entry.get("assignee", "") or meta.get("assignee", ""),
        "owner": entry.get("owner", "") or meta.get("owner", ""),
        "scope": None,
        "created_at": entry.get("created_at", "") or entry.get("date", ""),
        "updated_at": entry.get("updated_at", "") or entry.get("done_at", "") or entry.get("created_at", ""),
        "url_hash": f"#{ms_id or op_id}#{entry.get('id', '')}" if (ms_id or op_id) else f"#{entry.get('id', '')}",
    }


def _render_push(p: dict, q_lower: str) -> dict:
    return {
        "entity_type": "push",
        "id": p.get("id", ""),
        "title": p.get("summary", "") or p.get("description", ""),
        "snippet": _snippet(p.get("summary") or p.get("description") or "", q_lower),
        "ms_id": p.get("ms_id"),
        "op_id": None,
        "status": "",
        "priority": "",
        "created_at": p.get("pushed_at", "") or p.get("created_at", ""),
        "updated_at": p.get("pushed_at", "") or p.get("created_at", ""),
        "url_hash": f"#push-{p.get('id', '')}",
    }


def _render_deploy(d: dict, q_lower: str) -> dict:
    return {
        "entity_type": "deploy",
        "id": d.get("id", ""),
        "title": d.get("description", "") or d.get("desc", ""),
        "snippet": _snippet(d.get("description") or d.get("desc") or "", q_lower),
        "ms_id": d.get("ms_id"),
        "op_id": None,
        "status": d.get("status", ""),
        "priority": "",
        "created_at": d.get("deployed_at", "") or d.get("date", ""),
        "updated_at": d.get("deployed_at", "") or d.get("date", ""),
        "url_hash": f"#deploy-{d.get('id', '')}",
    }


def _render_target_generic(tgt: dict, kind: str, q_lower: str, *,
                           state_field: str = "status") -> dict:
    """Render a generic Target (opportunity / account / release / data-defined)
    into the uniform search-result shape (ms-109 e-5524).

    Occupation-agnostic: the display name is read via work_model.target_label
    (canonical ``label`` → legacy ``title`` / ``name``) so this one renderer
    serves every Target class without a dedicated renderer, no branching. The
    rendered ``status`` reads the class's declared ``state_field`` (dev = status,
    a funnel class = phase) — the SAME field the status filter matches on, so a
    caller can re-filter by the value it sees. entity_type is the class ``kind``
    so facets / the UI group by it; url_hash is ``#<kind>/<id>``."""
    label = work_model.target_label(tgt)
    src = tgt.get("description") or tgt.get("objective") or label
    tid = tgt.get("id", "")
    return {
        "entity_type": kind,
        "id": tid,
        "title": label,
        "snippet": _snippet(src, q_lower),
        "ms_id": None,
        "op_id": None,
        "status": tgt.get(state_field, ""),
        "priority": tgt.get("priority", ""),
        "assignee": tgt.get("assignee", ""),
        "owner": tgt.get("owner", ""),
        "scope": None,
        "created_at": tgt.get("created_at", ""),
        "updated_at": tgt.get("updated_at", "") or tgt.get("created_at", ""),
        "url_hash": f"#{kind}/{tid}" if tid else "",
    }


# Target classes that search walks with a DEDICATED renderer + scope semantics
# (milestone carries ms scope, operation carries op scope) rather than the generic
# loop. Its keys are the single source for "which kinds the hardcoded loops above
# handle" — the generic loop skips exactly these, so adding a dedicated loop and
# forgetting to exclude it here (double-render) or vice-versa (vanish) can't drift
# from a free-standing literal (ms-109 e-5524, maintainability review PR#683). The
# strangler's end state folds these into the generic walk too, deleting this map.
_DEDICATED_TARGET_RENDERERS = {
    "milestone": _render_milestone,
    "operation": _render_operation,
}


def _render_document(doc: dict, q_lower: str) -> dict:
    content = doc.get("content", "") or ""
    return {
        "entity_type": "document",
        "id": doc.get("doc_id", "") or doc.get("id", ""),
        "title": doc.get("title", ""),
        "snippet": _snippet(content, q_lower),
        "ms_id": doc.get("milestone"),
        "op_id": doc.get("operation"),
        "status": None,
        "priority": None,
        "scope": doc.get("scope", ""),
        "created_at": doc.get("created_at", ""),
        "updated_at": doc.get("updated_at", "") or doc.get("created_at", ""),
        "url_hash": f"#doc/{doc.get('doc_id', '') or doc.get('id', '')}",
    }


# ---------------------------------------------------------------------------
# Snippet extraction (for hit display)
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


def _first_line(text: str, limit: int = 120) -> str:
    if not text:
        return ""
    line = text.split("\n", 1)[0]
    return line[:limit]


# ---------------------------------------------------------------------------
# Facets
# ---------------------------------------------------------------------------

def _build_facets(matches: list[dict]) -> dict:
    facets = {"type": {}, "status": {}, "priority": {}, "scope": {}}
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
