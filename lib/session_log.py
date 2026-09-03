"""Beacon Session Log aggregation core (ms-57 / e-1037 + e-1038 + e-1039).

This module owns the single function that both ``beacon session end``
(self-aggregation at session-end) and ``beacon session rescue``
(orphan recovery at session-start) call. Per SPEC §3 the aggregation is
**idempotent** — running it twice on the same ``session_id`` produces
the same logical result (last writer wins per field, but no data is
destroyed). That property is what lets the rescue path safely operate
on live sessions: even if we "steal" an alive session's notes, the
alive side's session-end will overwrite us with a richer summary.

Schema (server/firestore_client.SESSION_LOGS_SUBCOLLECTION)::

    {
      session_id, summary, note_ids[], commit_ids[], pr_ids[],
      created_at, last_aggregated_at, recovered
    }

* ``summary`` is the durable content — it MUST survive the GC of any
  linked entry. Callers that have AI summarisation (e.g. the
  ``/beacon-session-end`` Skill) pass it via ``summary_override``;
  otherwise this module produces a mechanical summary from raw entry
  text so the entry still carries something readable.
* ``*_ids`` are best-effort back-references for short-term traceability.
* ``recovered`` is set on the **first** upsert from the rescue path only,
  so forensics can tell rescue-born entries from session-end ones.

Why a separate module: the aggregation logic is non-trivial (walks
nested entry trees, talks to two storage backends) and is consumed by
two different CLI entry points + future Skills. Centralising it here
keeps ``commands.py`` thin and locks the idempotency contract in one
place.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iter_entries(entries: list) -> list:
    """Flatten a (possibly nested) entry list. PR entries can contain child
    commit entries (lib/core.pr_add), and tasks can contain follow-up
    commits attached after _find_matching_task. The aggregation query
    must see both layers, so we walk the tree."""
    out = []
    for e in entries or []:
        out.append(e)
        nested = e.get("entries")
        if isinstance(nested, list):
            out.extend(_iter_entries(nested))
    return out


def collect_project_entries(data: dict, session_id: str) -> dict:
    """Walk every Target's entries and return per-type id lists matching
    ``meta.session_id == session_id``.

    Returns ``{commit_ids: [...], pr_ids: [...], commit_texts: [...], pr_texts: [...]}``.
    The ``*_texts`` lists power mechanical summary fallback so callers that
    don't have an AI on hand still produce a non-empty summary.

    ms-108 e-3269/e-3701 — occupation-neutral. Previously this walked only
    ``data["milestones"]`` (development Targets), so a sales project aggregated
    nothing into its session log. This function is a ③ shared-frame aggregator
    (it projects the occupation's Targets into a session log), NOT pure L1 —
    the earlier ① classification was wrong (fable review B-1). It now asks the
    occupation registry for the raw Target records instead of hardcoding the
    collection names, so occupation knowledge stays in one place
    (``occupation.iter_target_records``) rather than leaking into this module.

    (Mapping the sales work-record — Communication/証跡 — into the commit slot
    of the session log is a separate follow-up ``e-3702``: sales entries do not
    stamp ``meta.session_id`` yet, so there is nothing to collect from them
    here today.)
    """
    commit_ids: list[str] = []
    commit_texts: list[str] = []
    pr_ids: list[str] = []
    pr_texts: list[str] = []
    # ms-153 e-5550: the distinct Targets this session's entries landed on, in
    # first-seen order. session-end uses this to scope its summary write to the
    # ACTUAL worked target (SPEC 方針3: write = 作業した target) instead of a
    # project-wide log. Sourced from the SAME walk as the commit/pr collection so
    # attribution never diverges from what was collected.
    target_ids: list[str] = []

    import occupation
    # ARM-NAME COUPLING (ms-142 e-5012, tracked debt): target ENUMERATION is
    # abstracted (iter_target_records), but the dev ``entries`` arm is still read
    # by name here, so a sales Opportunity's Communication evidence (arm
    # ``communications``) is missed. The fix is to route through
    # occupation.profession_manifest evidence_arms; deferred with e-3702 (sales
    # records do not stamp meta.session_id yet). Registered as pending debt in
    # capability_ledger.KNOWN_ARM_REACH ("session_log", "entries") — the checker's
    # stale-entry test forces this comment + read to be remediated together.
    for tgt in occupation.iter_target_records(data):
        tid = tgt.get("id", "")
        matched = False
        for e in _iter_entries(tgt.get("entries", [])):
            meta = e.get("meta") or {}
            if meta.get("session_id") != session_id:
                continue
            etype = e.get("type")
            eid = e.get("id", "")
            if etype == "commit":
                commit_ids.append(eid)
                msg = meta.get("message") or e.get("description") or ""
                commit_texts.append(msg)
                matched = True
            elif etype == "pr":
                pr_ids.append(eid)
                title = e.get("description") or meta.get("url") or ""
                pr_texts.append(title)
                matched = True
        if matched and tid and tid not in target_ids:
            target_ids.append(tid)

    return {
        "commit_ids": commit_ids,
        "commit_texts": commit_texts,
        "pr_ids": pr_ids,
        "pr_texts": pr_texts,
        "target_ids": target_ids,
    }


def collect_local_notes(beacon_dir: Path, session_id: str) -> dict:
    """Read .beacon/session_notes.jsonl and return entries for the session.

    Note IDs in local mode are the timestamps themselves — notes have no
    other stable identifier in jsonl. SPEC §3 calls these refs "best
    effort", so a ts-keyed list is sufficient for the aggregation
    traceability use case.
    """
    note_ids: list[str] = []
    note_texts: list[str] = []
    path = beacon_dir / "session_notes.jsonl"
    if not path.exists():
        return {"note_ids": note_ids, "note_texts": note_texts}
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    note = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if note.get("session_id") != session_id:
                    continue
                ts = note.get("ts", "")
                if ts:
                    note_ids.append(ts)
                text = note.get("text", "")
                if text:
                    note_texts.append(text)
    except OSError:
        pass
    return {"note_ids": note_ids, "note_texts": note_texts}


def collect_cloud_notes(client, project_id: str, session_id: str) -> dict:
    """Fetch session notes from the cloud API and filter by session_id.

    Returns the same shape as :func:`collect_local_notes`. The cloud
    endpoint returns whatever schema the server stores; we tolerate
    missing ``session_id`` (older notes) by simply skipping them.
    """
    note_ids: list[str] = []
    note_texts: list[str] = []
    try:
        notes = client.list_notes(project_id)
    except Exception:
        return {"note_ids": note_ids, "note_texts": note_texts}
    for note in notes or []:
        if note.get("session_id") != session_id:
            continue
        ts = note.get("ts", "")
        if ts:
            note_ids.append(ts)
        text = note.get("text", "")
        if text:
            note_texts.append(text)
    return {"note_ids": note_ids, "note_texts": note_texts}


def mechanical_summary(
    *,
    note_texts: list[str],
    commit_texts: list[str],
    pr_texts: list[str],
    max_chars: int = 600,
) -> str:
    """Produce a fallback summary from raw entry text.

    Used when no AI-generated summary is supplied. Keeps the session log
    entry meaningful even in CLI-only contexts (no Skill). The exact
    format is "N notes / M commits / K PRs: <first chars of joined text>".
    Truncated to ``max_chars`` to keep the document small.
    """
    parts = []
    if note_texts:
        parts.append(f"{len(note_texts)} notes")
    if commit_texts:
        parts.append(f"{len(commit_texts)} commits")
    if pr_texts:
        parts.append(f"{len(pr_texts)} PRs")
    header = " / ".join(parts) if parts else "no entries"

    joined = " | ".join(
        [t for t in (note_texts + commit_texts + pr_texts) if t]
    )
    if len(joined) > max_chars:
        joined = joined[: max_chars - 1] + "…"
    return f"{header}: {joined}" if joined else header


# ---------------------------------------------------------------------------
# Aggregation core
# ---------------------------------------------------------------------------

def _read_fork_target_id(beacon_dir: Path) -> str:
    """Return ``.beacon/fork.json``'s ``target_ms_id`` (the Target a fork worktree
    exists to advance), or "" when there is no fork / it is malformed / the field
    is empty. Read-only — never writes fork.json.

    This is the filesystem half of worked-target resolution; the RULE that turns
    (fork target + the session's entry targets) into the worked-target SET lives
    in the pure, occupation-generic ``occupation.resolve_worked_targets`` (ms-164
    実装順序1), which this module feeds. Kept tiny and local so ``aggregate_session``
    reads the fork hint the same way ``cmd_log._read_fork_target_ms_id`` does."""
    fork_path = beacon_dir / "fork.json"
    if not fork_path.exists():
        return ""
    try:
        fork = json.loads(fork_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # malformed / unreadable fork.json → treat as no fork hint
        return ""
    if not isinstance(fork, dict):
        return ""
    return (fork.get("target_ms_id") or "").strip()


def aggregate_session(
    *,
    project_data: dict,
    beacon_dir: Path,
    session_id: str,
    recovered: bool = False,
    summary_override: Optional[str] = None,
    cloud_client: Any = None,
    cloud_project_id: str = "",
    existing_log: Optional[dict] = None,
) -> dict:
    """Build the session log payload for a given session_id.

    This function is **pure** with respect to filesystem / cloud writes:
    it collects inputs, computes the payload, but does not persist it.
    The caller (CLI command) decides whether to write local-only or
    push to the cloud session log endpoint. That split keeps the
    function unit-testable without a Firestore stub.

    ``recovered`` flag semantics (per SPEC §7):
      * Set True only on the **first** rescue-born upsert.
      * If ``existing_log`` already has ``recovered: True`` we preserve it.
      * If ``existing_log`` exists without recovered (i.e. session-end
        already produced it), we never flip it back to True.
      * If ``existing_log`` is None and the caller is a rescue path,
        ``recovered=True`` becomes part of the new payload.

    ``cloud_client`` is optional. When supplied, notes are pulled from
    the cloud endpoint (authoritative when cloud mode is on). When
    omitted, we fall back to the local jsonl.
    """
    if cloud_client and cloud_project_id:
        notes = collect_cloud_notes(cloud_client, cloud_project_id, session_id)
    else:
        notes = collect_local_notes(beacon_dir, session_id)

    proj_entries = collect_project_entries(project_data, session_id)

    summary = summary_override or mechanical_summary(
        note_texts=notes["note_texts"],
        commit_texts=proj_entries["commit_texts"],
        pr_texts=proj_entries["pr_texts"],
    )

    # ms-153 e-5550 → ms-164 e-5942 (SPEC 方針3): attribute the summary to the
    # Target(s) this session actually advanced instead of an implicit project-wide
    # log. ms-164 makes this MULTI: a session that touched several Targets in a day
    # attributes to ALL of them (``target_ids``), so the record is reachable from
    # the root AND from each child Target — the older single-target collapse folded
    # a cross-target session to ``ambiguous`` → unattributed. The resolution RULE is
    # the pure, occupation-generic ``occupation.resolve_worked_targets`` (実装順序1):
    # fork ∪ entries → active fallback → none. This module only supplies the fork
    # hint (filesystem) and the entry Targets (the same walk that collected the
    # commits, so attribution never diverges from what was collected).
    import occupation  # local import mirrors collect_project_entries (no top-level cycle)
    worked = occupation.resolve_worked_targets(
        project_data,
        entry_target_ids=proj_entries.get("target_ids", []),
        fork_target_id=_read_fork_target_id(beacon_dir),
    )
    worked_ids = worked["target_ids"]

    now = _now_iso()
    payload = {
        "session_id": session_id,
        "summary": summary,
        "note_ids": notes["note_ids"],
        "commit_ids": proj_entries["commit_ids"],
        "pr_ids": proj_entries["pr_ids"],
        # ``target_ids`` is the multi-attribution set (ms-164 e-5942). ``target_id``
        # stays as the first of that set for back-compat with readers that predate
        # multi (session-end Skill / any legacy consumer) — the read-side symmetric
        # migration onto ``target_ids`` is a following slice.
        "target_ids": worked_ids,
        "target_id": worked_ids[0] if worked_ids else "",
        "target_source": worked["target_source"],
        "last_aggregated_at": now,
    }

    if existing_log is None:
        # First upsert ever for this session_id.
        payload["created_at"] = now
        if recovered:
            payload["recovered"] = True
    else:
        # Preserve recovered flag from prior state — never flip a True to
        # False, never set True if session-end has already claimed it.
        if existing_log.get("recovered") is True:
            payload["recovered"] = True
        # Don't overwrite created_at on re-aggregation; let server merge
        # keep the original.

    return payload


__all__ = [
    "aggregate_session",
    "collect_cloud_notes",
    "collect_local_notes",
    "collect_project_entries",
    "mechanical_summary",
]
