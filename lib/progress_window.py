"""「期間内に進捗した対象」の導出 — the ONE definition (ms-184 / e-7015).

What this answers
=================
"Which Targets actually moved between date A and date B, and what moved them?"

Three different consumers ask that same question and **must not each own a
copy** of the answer (ms-184 SPEC 方針 5):

1. one project's detail  — ``beacon recap targets``        (e-7015)
2. a named project's detail, read without leaving the current one
                           — ``beacon recap targets --project <id>`` (e-7020)
3. every owned project, aggregated
                           — ``beacon recap projects``     (e-7017)

:func:`derive` is that single definition; :func:`aggregate` folds many
``derive`` results into the cross-project answer. Nothing here does I/O — the
caller (CLI in local mode, CLI over the API, or the server) hands in the
project dict, its documents, and optionally its decision records.

Why evidence, not the Target's own date
=======================================
The obvious implementation — filter Targets by their own ``created_at`` —
answers a *different* question ("which Targets were created then") and was
measurably wrong: on 2026-10-15 it returned 25 long-finished Targets for every
period asked, and never returned the Target that took 8 commits the day
before. Progress percentage is no better: it is the AI's own self-report, and
a Target showed 100% with 15 work items still open the same day.

So progress is derived from **what got attached to the Target during the
period** — commits, PRs, work items raised or finished, documents written,
completion approvals, recorded decisions. A Target's own creation timestamp is
deliberately NOT evidence (ms-184 受入条件 1); in practice raising a Target
also raises its work items, which *are* evidence.

Undated records
===============
Records carrying no timestamp at all cannot be judged against a period. They
are excluded from the evidence AND counted in ``undated`` so the caller can
say "N records could not be judged" instead of implying they did not happen
(ms-184 SPEC 方針 8, via :mod:`date_window`).
"""

from __future__ import annotations

from typing import Optional

import date_window as _dw
import occupation as _occ
import work_model as _wm

#: Timestamp fields a record may carry, in the order they are reported. Each is
#: a distinct moment at which the record moved, so all are candidates and the
#: newest one inside the window dates the evidence.
_STAMP_FIELDS = ("done_at", "created_at", "date", "pushed_at", "deployed_at",
                 "updated_at", "approved_at")

#: Entry types that are NOT evidence of the Target moving. ``task`` IS evidence
#: (raising or finishing a work item is movement), so this set stays tiny and
#: explicit rather than an allowlist that silently drops newly-introduced types
#: — a new entry type should count as movement by default.
_NON_EVIDENCE_TYPES = frozenset({
    # A note/comment attached to a Target records a thought, not a change to
    # the work. Keep this list justified, one line per entry.
})

#: The evidence sources :func:`derive` can consult. Reported back so a caller
#: can tell "nothing happened" from "we did not look" (方針 8 again).
EVIDENCE_SOURCES = ("entry", "document", "decision")


def _stamps(record: dict) -> list:
    """Every timestamp ``record`` carries, newest-first ordering not assumed."""
    out = []
    for f in _STAMP_FIELDS:
        v = record.get(f)
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
    return out


def _summary(record: dict) -> str:
    for f in ("description", "title", "label", "summary"):
        v = record.get(f)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _arms_for(project: dict, collection: str, all_arms: tuple) -> tuple:
    """The child-record arms to walk for ``collection``.

    Normally the class's DECLARED arms (``occupation.target_decomposition``),
    so a sales Opportunity's activities / communications are evidence by the
    same rule a dev Milestone's entries are — no arm name is written here, and
    declaring a new occupation lights this derivation up with no edit
    (ms-142's "declare ⇒ light up" contract).

    A class that declares NO arms falls back to the union of every known
    child-table name. That is not laziness: a development Operation
    deliberately has undeclared arms (ms-142 e-5156 / T1 ruling) while its
    records physically carry run records and incidents, so a strict read of the
    declaration would silently drop every Operation's movement. The fallback
    is still declaration-derived (``target_child_tables``) rather than a
    hardcoded ``"entries"``, so it cannot rot into profession coupling.
    """
    declared = tuple((_occ.target_decomposition(project).get(collection)
                      or {}).get("arms") or ())
    return declared or all_arms


def _walk_arms(record: dict, arms: tuple, out, task_index, target_id):
    """Flatten a Target's child records (and their nesting) into ``out``.

    Recurses through the SAME arm names at every depth, which is how a dev
    Milestone's ``entries``-within-``entries`` tree (a PR's commits nested
    under it) is reached without naming the arm.

    ``task_index`` maps every child record id under this Target to the Target
    id, so a decision that references a work item is attributed to the Target
    owning it without a second pass.
    """
    for arm in arms:
        for item in record.get(arm) or []:
            if not isinstance(item, dict):
                continue
            iid = item.get("id") or ""
            if iid:
                task_index[iid] = target_id
            out.append(item)
            _walk_arms(item, arms, out, task_index, target_id)


def _target_rows(project: dict) -> list:
    """Every Target record with its kind and arms, occupation-generically.

    Walks ``occupation.target_collections`` — the declared registry — rather
    than a concrete collection name, so a sales project's opportunities and any
    data-defined class are derived by the same rule as a dev project's
    milestones, with no profession branch here.
    """
    all_arms = tuple(_occ.target_child_tables(project))
    rows = []
    for coll in _occ.target_collections(project):
        kind = _occ.collection_kind(project, coll,
                                    include_non_aggregatable=True) or ""
        arms = _arms_for(project, coll, all_arms)
        for rec in project.get(coll, []) or []:
            if isinstance(rec, dict) and rec.get("id"):
                rows.append((rec, kind or _wm.target_kind(rec.get("id", "")),
                             arms))
    return rows


def derive(
    project: dict,
    *,
    from_date: str = "",
    to_date: str = "",
    documents: Optional[list] = None,
    decisions: Optional[list] = None,
    evidence_limit: int = 5,
) -> dict:
    """Return the Targets of ``project`` that progressed within the window.

    ``documents`` / ``decisions`` are optional because not every caller can
    reach them (the decision stream is cloud-only). Whichever is ``None`` is
    reported in ``sources_unavailable`` rather than treated as empty, so a
    zero result is never mistaken for a complete one.

    ``evidence_limit`` caps the evidence rows carried per Target (0 = all);
    ``evidence_count`` is always the full count, so a capped list never
    understates how much moved.
    """
    window = {"from": (from_date or "").strip(), "to": (to_date or "").strip()}

    consulted = ["entry"]
    unavailable = []
    for name, value in (("document", documents), ("decision", decisions)):
        (consulted if value is not None else unavailable).append(name)

    # ---- index every Target's entries once -------------------------------
    task_index: dict = {}
    per_target: dict = {}
    order: list = []
    for rec, kind, arms in _target_rows(project):
        tid = rec.get("id")
        if tid in per_target:
            continue
        flat: list = []
        _walk_arms(rec, arms, flat, task_index, tid)
        per_target[tid] = {
            "target_id": tid,
            "target_kind": kind,
            "label": _wm.target_label(rec) or tid,
            "status": rec.get("status", "") or rec.get("phase", ""),
            "_entries": flat,
            "evidence": [],
        }
        order.append(tid)

    undated_by_kind: dict = {}

    def _note_undated(kind: str) -> None:
        undated_by_kind[kind] = undated_by_kind.get(kind, 0) + 1

    def _add(tid: str, kind: str, rec_id: str, at: str, summary: str) -> None:
        row = per_target.get(tid)
        if row is None:
            return
        row["evidence"].append({
            "kind": kind, "id": rec_id, "at": at,
            "summary": summary[:160],
        })

    # ---- evidence source 1: child records on the Target's declared arms --
    for tid, row in per_target.items():
        for e in row["_entries"]:
            etype = (e.get("type") or "").strip() or "entry"
            if etype in _NON_EVIDENCE_TYPES:
                continue
            stamps = _stamps(e)
            verdict = _dw.classify_any(stamps, window["from"], window["to"])
            if verdict == _dw.UNDATED:
                _note_undated(etype)
                continue
            if verdict != _dw.IN_WINDOW:
                continue
            _add(tid, etype, e.get("id", ""),
                 _dw.latest_in_window(stamps, window["from"], window["to"]),
                 _summary(e))

    # ---- evidence source 2: documents linked to the Target --------------
    for doc in documents or []:
        if not isinstance(doc, dict):
            continue
        tid = _wm.doc_target(doc)
        if not tid or tid not in per_target:
            continue
        stamps = _stamps(doc)
        verdict = _dw.classify_any(stamps, window["from"], window["to"])
        if verdict == _dw.UNDATED:
            _note_undated("document")
            continue
        if verdict != _dw.IN_WINDOW:
            continue
        _add(tid, "document", doc.get("doc_id") or doc.get("id", ""),
             _dw.latest_in_window(stamps, window["from"], window["to"]),
             doc.get("title", ""))

    # ---- evidence source 3: recorded decisions --------------------------
    for dec in decisions or []:
        if not isinstance(dec, dict):
            continue
        related = dec.get("related") or {}
        tid = (related.get("target_id") or "").strip()
        if not tid:
            tid = task_index.get((related.get("task_id") or "").strip(), "")
        if not tid or tid not in per_target:
            continue
        stamps = _stamps(dec)
        verdict = _dw.classify_any(stamps, window["from"], window["to"])
        if verdict == _dw.UNDATED:
            _note_undated("decision")
            continue
        if verdict != _dw.IN_WINDOW:
            continue
        _add(tid, "decision", dec.get("decision_id", ""),
             _dw.latest_in_window(stamps, window["from"], window["to"]),
             dec.get("what") or dec.get("decision") or dec.get("rationale", ""))

    # ---- assemble --------------------------------------------------------
    targets = []
    evidence_total = 0
    for tid in order:
        row = per_target[tid]
        row.pop("_entries", None)   # working index, not part of the answer
        evidence = sorted(row["evidence"], key=lambda r: r["at"], reverse=True)
        if not evidence:
            continue
        evidence_total += len(evidence)
        by_kind: dict = {}
        for item in evidence:
            by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + 1
        targets.append({
            "target_id": row["target_id"],
            "target_kind": row["target_kind"],
            "label": row["label"],
            "status": row["status"],
            "evidence_count": len(evidence),
            "evidence_by_kind": by_kind,
            "last_evidence_at": evidence[0]["at"],
            "evidence": evidence if evidence_limit <= 0 else evidence[:evidence_limit],
        })

    targets.sort(key=lambda r: r["last_evidence_at"], reverse=True)

    return {
        "window": window,
        "moved": bool(targets),
        "targets": targets,
        "target_count": len(targets),
        "evidence_count": evidence_total,
        "last_evidence_at": targets[0]["last_evidence_at"] if targets else "",
        "undated": {
            "count": sum(undated_by_kind.values()),
            "by_kind": undated_by_kind,
        },
        "sources_consulted": [s for s in EVIDENCE_SOURCES if s in consulted],
        "sources_unavailable": [s for s in EVIDENCE_SOURCES if s in unavailable],
    }


def aggregate(rows, *, from_date: str = "", to_date: str = "") -> dict:
    """Fold per-project :func:`derive` results into the cross-project answer.

    ``rows`` is an iterable of ``{"project_id", "name", "result"}`` for
    projects that were read, and/or ``{"project_id", "name", "error"}`` for
    projects that could NOT be read. Projects that did not move are dropped
    (ms-184 SPEC 方針 2 — a list where every project looks the same is the
    problem this replaces), but projects we **failed to read** are kept in
    ``unavailable``: "nothing happened" and "we could not tell" must stay
    distinguishable (受入条件 8).
    """
    moved = []
    unavailable = []
    for row in rows or []:
        pid = (row.get("project_id") or "").strip()
        name = row.get("name") or pid
        if row.get("error"):
            unavailable.append({
                "project_id": pid, "name": name, "error": str(row["error"]),
            })
            continue
        result = row.get("result") or {}
        if not result.get("moved"):
            continue
        moved.append({
            "project_id": pid,
            "name": name,
            "target_count": result.get("target_count", 0),
            "evidence_count": result.get("evidence_count", 0),
            "last_evidence_at": result.get("last_evidence_at", ""),
            "targets": [
                {
                    "target_id": t["target_id"],
                    "target_kind": t["target_kind"],
                    "label": t["label"],
                    "status": t["status"],
                    "evidence_count": t["evidence_count"],
                    "last_evidence_at": t["last_evidence_at"],
                }
                for t in result.get("targets", [])
            ],
        })
    moved.sort(key=lambda r: r["last_evidence_at"], reverse=True)
    return {
        "window": {"from": (from_date or "").strip(), "to": (to_date or "").strip()},
        "projects": moved,
        "project_count": len(moved),
        "unavailable": unavailable,
    }


__all__ = ["EVIDENCE_SOURCES", "derive", "aggregate"]
