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

import json

from typing import Optional

import date_window as _dw
import occupation as _occ
import work_model as _wm

#: Which stamps count as "this record moved" — the DECLARED movement set
#: (``date_window.MOVEMENT_STAMPS``), not a list private to this module. Its
#: sibling ``CREATION_STAMPS`` answers the other period question ("raised in
#: this window", what ``search`` means); keeping both named in one place is
#: what makes the difference between them a stated choice instead of an
#: accident two files apart (maintainability review PR#803).
_STAMP_FIELDS = _dw.MOVEMENT_STAMPS

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
EVIDENCE_SOURCES = ("entry", "document", "decision", "shipping")

#: Project-level records of SHIPPING — pushing, deploying, cutting a release.
#:
#: These do not live on a Target's arm; they sit directly on the project, each
#: naming the Target(s) it carried. Leaving them out meant the derivation could
#: only see work that landed *under* a Target, so a day whose whole story was
#: "we put it in production" read as "nothing happened" (ms-184 / e-7055).
#: Measured before this change: of 32 days with a deploy, the recap mentioned
#: the deploy on **zero** of them, and one day (5 deploys, nothing attached to
#: a Target) came back as 0 件 — a confident wrong answer to the one question
#: this milestone exists to answer.
#:
#: Each entry declares only where the collection lives, what to call the
#: evidence, and which fields date it. It deliberately does NOT name the field
#: that holds the Target ids: the first version did (``milestones``), and this
#: module's own guard caught that as profession coupling — a deploy's
#: ``milestones`` field is dev-era schema, so a sales project's shipping record
#: would never match it. :func:`_shipping_targets` discovers the attribution by
#: looking for values that ARE known Target ids instead, which works for any
#: class without this table knowing one.
#:
#: ``via`` is the one structural hop: a release names its deploys, and the
#: deploys name the Targets.
SHIPPING_SOURCES = (
    {"collection": "pushes", "kind": "push",
     "stamps": ("pushed_at", "created_at")},
    {"collection": "deployments", "kind": "deploy",
     "stamps": ("deployed_at", "date")},
    {"collection": "deploys", "kind": "deploy",      # legacy spelling
     "stamps": ("deployed_at", "date")},
    {"collection": "releases", "kind": "release",
     "stamps": ("date", "created_at"),
     "via": {"collection": ("deployments", "deploys"), "id_list": "deploy_ids"}},
)


def _stamps(record: dict) -> list:
    """Every movement timestamp ``record`` carries (ordering not assumed)."""
    return _dw.stamps_of(record, _STAMP_FIELDS)


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


#: Fields of a SHIPPING record that are never attribution, whatever they hold.
#:
#: Attribution is found by value (see :func:`_target_ids_in`), which is what
#: keeps it occupation-generic. But value-matching alone is not a guarantee —
#: an independent review constructed a push whose git author name happened to
#: be ``ms-7`` and watched it get attributed to that milestone (PR#810). The
#: first docstring here claimed sibling fields "simply do not resolve"; that
#: was a coincidence about id shapes, asserted as a property.
#:
#: These names are fields of the project-level shipping records themselves
#: (author, hashes, branch, free text, version) — NOT profession concretes, so
#: excluding them by name introduces no occupation coupling. The residual risk
#: is stated honestly in :func:`_target_ids_in`: a field not on this list whose
#: value collides with a Target id still resolves.
NON_ATTRIBUTION_FIELDS = frozenset({
    "pushed_by", "from_hash", "to_hash", "branch", "git_hash",
    "commit_hashes", "commits", "description", "summary", "semver",
    "environment", "type", "id", "linked_release", "unassigned_commits",
})


def _target_ids_in(record: dict, known_targets) -> list:
    """Target ids this record carries, in any field not excluded by name.

    Finds attribution by VALUE, not by a positive list of field names. A deploy
    happens to use ``milestones`` today, but that field name is dev-era schema;
    naming it here would mean a sales project's shipping record could never be
    attributed, and this module's own coupling guard rightly rejects the
    literal. Matching against the project's real Target ids needs no field name
    and works for every class.

    **The residual risk, stated plainly**: this is not a structural guarantee.
    Fields known never to carry attribution are excluded by name
    (``NON_ATTRIBUTION_FIELDS``), which covers the shipping records' own author
    / hash / free-text / version fields. A field OUTSIDE that set whose value
    happens to equal a Target id will still be read as attribution. That is a
    coincidence about id shapes, not something this function prevents — so if
    the Target id scheme ever becomes short or human-word-like, revisit this.
    """
    found = []
    for field, value in (record or {}).items():
        if field in NON_ATTRIBUTION_FIELDS:
            continue
        if isinstance(value, str):
            if value in known_targets:
                found.append(value)
        elif isinstance(value, (list, tuple)):
            found.extend(v for v in value
                         if isinstance(v, str) and v in known_targets)
    return found


def _shipping_targets(project: dict, record: dict, spec: dict,
                      known_targets) -> list:
    """The Target ids a shipping record names, following any declared hop.

    Returns ``[]`` when the record names none — a real state, not an error:
    measured on this project, 13 of 958 pushes and 7 of 65 deploys carry no
    attribution. The caller keeps those as ``unattributed`` rather than
    dropping them or picking a Target for them (e-7055 受入条件 3, the same
    rule the date logic uses for a record it cannot judge).
    """
    direct = _target_ids_in(record, known_targets)
    if direct:
        return direct
    via = spec.get("via") or {}
    if not via:
        return []
    # A release names deploys, and the deploys name the Targets. Walking the
    # hop keeps a release attributed instead of permanently unattributed.
    wanted = {i for i in (record.get(via.get("id_list")) or [])
              if isinstance(i, str)}
    out = []
    for coll in via.get("collection", ()):
        for dep in project.get(coll, []) or []:
            if isinstance(dep, dict) and dep.get("id") in wanted:
                out.extend(_target_ids_in(dep, known_targets))
    return out


def _walk_shipping(project: dict, from_date: str, to_date: str,
                   known_targets) -> tuple:
    """Return ``(per_target, unattributed, undated)`` for the shipping records.

    ``per_target`` maps a Target id to the evidence rows it carried;
    ``unattributed`` holds the in-window records that named no reachable
    Target. Both are reported — the project moved either way, and which Target
    moved is a separate question from whether anything shipped.
    """
    per_target: dict = {}
    unattributed: list = []
    undated: dict = {}
    seen: set = set()

    for spec in SHIPPING_SOURCES:
        for rec in project.get(spec["collection"], []) or []:
            if not isinstance(rec, dict):
                continue
            rid = rec.get("id") or ""
            # ``deployments`` / ``deploys`` are two spellings of one collection;
            # a project carrying both must not count a record twice. Keying the
            # de-dup on the id ALONE silently gave up on records that have no
            # id — legacy data does — and an independent review reproduced the
            # double count with exactly that shape (PR#810). The fallback key
            # is the record's own content, so an id-less record de-dups too.
            dedup = (spec["kind"], rid) if rid else (
                spec["kind"], tuple(sorted(
                    (k, json.dumps(v, ensure_ascii=False, sort_keys=True)
                     if isinstance(v, (list, dict)) else v)
                    for k, v in rec.items())))
            if dedup in seen:
                continue
            seen.add(dedup)

            stamps = _dw.stamps_of(rec, spec["stamps"])
            verdict = _dw.classify_any(stamps, from_date, to_date)
            if verdict == _dw.UNDATED:
                undated[spec["kind"]] = undated.get(spec["kind"], 0) + 1
                continue
            if verdict != _dw.IN_WINDOW:
                continue

            row = {
                "kind": spec["kind"],
                "id": rid,
                "at": _dw.latest_in_window(stamps, from_date, to_date),
                "summary": _summary(rec)[:160],
            }
            tids = _shipping_targets(project, rec, spec, known_targets)
            if tids:
                for tid in dict.fromkeys(tids):   # de-dup, keep order
                    per_target.setdefault(tid, []).append(dict(row))
            else:
                unattributed.append(row)

    return per_target, unattributed, undated


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

    consulted = ["entry", "shipping"]
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

    # ---- evidence source 4: project-level shipping records ---------------
    # Attributed rows join the Target that carried them; unattributed ones are
    # kept separately (e-7055). A deploy naming several Targets is attached to
    # each — folding it into one would misreport which work shipped.
    ship_per_target, unattributed, ship_undated = _walk_shipping(
        project, window["from"], window["to"], set(per_target))
    for kind, count in ship_undated.items():
        _note_undated(kind)
        for _ in range(count - 1):
            _note_undated(kind)
    for tid, rows in ship_per_target.items():
        for row in rows:
            _add(tid, row["kind"], row["id"], row["at"], row["summary"])

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

    unattributed.sort(key=lambda r: r["at"], reverse=True)
    unattributed_by_kind: dict = {}
    for row in unattributed:
        unattributed_by_kind[row["kind"]] = \
            unattributed_by_kind.get(row["kind"], 0) + 1

    # A day whose whole story is "we put it in production" moved, even when no
    # Target claims the deploy. Keying ``moved`` off the Target list alone was
    # how such a day came back as 0 件 (e-7055 受入条件 1). The two halves stay
    # separately countable, because "which Target moved" and "did anything
    # ship" are different questions.
    last_at = max(
        [t["last_evidence_at"] for t in targets]
        + [r["at"] for r in unattributed]
    ) if (targets or unattributed) else ""

    return {
        "window": window,
        "moved": bool(targets) or bool(unattributed),
        "targets": targets,
        "target_count": len(targets),
        # 期間内と**判定できた**証跡の総数 = 対象に紐づいた分 + 対象不明の出荷。
        # ``undated`` は含めない — あれは「期間で判定できなかった」ものなので、
        # 証跡として数えると「判定できた量」が嘘になる。この非対称は意図的で、
        # 名前から読めないという指摘 (AX review PR#810) に対し、分けて数えられる
        # ように attributed_evidence_count を併記する。
        "evidence_count": evidence_total + len(unattributed),
        "attributed_evidence_count": evidence_total,
        "last_evidence_at": last_at,
        # Shipping records that named no reachable Target. Neither dropped nor
        # assigned to an arbitrary Target — the project moved, and WHICH target
        # is simply unknown (受入条件 3).
        "unattributed": {
            "count": len(unattributed),
            "by_kind": dict(sorted(unattributed_by_kind.items())),
            "evidence": unattributed if evidence_limit <= 0
            else unattributed[:evidence_limit],
        },
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
    undated_total = 0
    unattributed_total = 0
    missing_sources: list = []
    for row in rows or []:
        pid = (row.get("project_id") or "").strip()
        name = row.get("name") or pid
        if row.get("error"):
            unavailable.append({
                "project_id": pid, "name": name, "error": str(row["error"]),
            })
            continue
        result = row.get("result") or {}
        # Accumulate the caveats BEFORE the mover filter. A project that looks
        # still may only look that way because a whole evidence source was
        # unreadable for it — dropping its caveat with the project is how
        # "could not tell" turns into "did not move" (受入条件 8). This is the
        # second consumer of the signal derive() emits; wiring only the first
        # one is the exact gap this MS is about.
        undated_total += int((result.get("undated") or {}).get("count") or 0)
        unattributed_total += int(
            (result.get("unattributed") or {}).get("count") or 0)
        for src in result.get("sources_unavailable") or []:
            if src not in missing_sources:
                missing_sources.append(src)
        if not result.get("moved"):
            continue
        moved.append({
            "project_id": pid,
            "name": name,
            "target_count": result.get("target_count", 0),
            "evidence_count": result.get("evidence_count", 0),
            "last_evidence_at": result.get("last_evidence_at", ""),
            # How much of this project's movement names no Target. A project
            # that moved ONLY by shipping is listed with target_count 0 and a
            # non-zero count here, rather than dropped for having no Targets.
            "unattributed_count": int(
                (result.get("unattributed") or {}).get("count") or 0),
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
        # Same two caveats derive() reports, folded across every project that
        # was read — including the ones omitted as non-movers.
        "undated": {"count": undated_total},
        "unattributed": {"count": unattributed_total},
        "sources_unavailable": [s for s in EVIDENCE_SOURCES if s in missing_sources],
    }


__all__ = ["EVIDENCE_SOURCES", "SHIPPING_SOURCES", "derive", "aggregate"]
