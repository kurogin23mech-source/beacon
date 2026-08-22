"""Migrate a local ``.beacon/project.json`` into the SQLite store (ms-148 e-5413).

Before the CLI can be switched onto the SQLite backend (e-5414), every existing
local project has to be moved into the new store — and the move has to be
*checkable*, because a migration that silently drops a milestone or garbles an
entry is worse than no migration (受入条件6: 前後で件数と内容が一致することを確認できる).

``migrate_json_to_sqlite`` reads the JSON document, writes it into a SqliteStore
via the same transactional ``apply`` used at runtime, then reads it back and
verifies the round trip. Verification is **order-insensitive**: the v3 store
re-sorts milestones (ms-N order) and entries (created_at order) on assemble, so a
positional diff would false-alarm. We compare by id instead — every milestone,
every entry (recursively, including inline children), and the non-milestone meta
must match. Any mismatch is reported, not swallowed.

This does not delete or replace the JSON file — it only produces the ``.db`` and
a report. Cutting the CLI over and removing the old path is e-5414.
"""

from __future__ import annotations

import json
from typing import Any

from store_sqlite import SqliteStore


def migrate_json_to_sqlite(project_file: str, db_path: str, *,
                           verify: bool = True) -> dict:
    """Load ``project_file`` (JSON) and write it into a SqliteStore at ``db_path``.

    Returns a report dict. When ``verify`` is True the report includes a
    ``verification`` block and a top-level ``verified`` bool; when the round trip
    does not match, ``verified`` is False and ``verification.issues`` lists every
    discrepancy (nothing is hidden). ``validate=False`` on the write so a project
    that is already slightly out of spec can still be moved and inspected rather
    than blocking the migration outright.
    """
    with open(project_file, "r", encoding="utf-8") as f:
        original = json.load(f)

    # Pass project_file so the store refreshes the read-only project.json mirror
    # to the canonical form during migration — otherwise the mirror would stay as
    # the raw pre-migration JSON while SQLite (the truth) holds the normalised
    # form, and readers of the mirror (e.g. Tauri, project export) would drift.
    store = SqliteStore(project_file, db_path=db_path)
    store.apply(lambda _current: (original, None), validate=False)

    # The report shape is CONSTANT across the verify / no-verify paths so a
    # caller never has to branch on which keys exist. ``verified`` is tri-state:
    # True (ran, matched) / False (ran, mismatch) / None (verify was skipped) —
    # so ``report.get("verified")`` cannot confuse a skipped check with a failed
    # one, and a bare ``report["verified"]`` never KeyErrors.
    report: dict[str, Any] = {"migrated": True, "db_path": db_path,
                              "project_file": project_file,
                              "verified": None, "verification": None}
    if verify:
        restored = store.load_project()
        verification = verify_migration(original, restored)
        report["verification"] = verification
        report["verified"] = verification["match"]
    return report


def _entry_index(entries: list) -> tuple[dict, list]:
    """Index a list of entries by id, recursively flattening inline children.

    Returns ``(index, duplicate_ids)``. The recursion mirrors v3_schema's data
    model, where a child entry (e.g. a commit under a task) lives INLINE in its
    parent's ``entries`` list rather than as its own row — so flattening here is
    how we compare the same tree the store round-trips. If v3_schema ever
    promotes children to their own rows, this recursion must change with it.

    Duplicate ids are surfaced (not silently overwritten): without this, two
    entries sharing an id would collapse to one in the dict and a real loss
    could pass verification unnoticed (the verifier must not rubber-stamp)."""
    out: dict[str, dict] = {}
    duplicates: list[str] = []

    def walk(items: list) -> None:
        for e in items or []:
            eid = e.get("id")
            if eid:
                if eid in out:
                    duplicates.append(eid)
                out[eid] = e
            walk(e.get("entries", []) or [])

    walk(entries)
    return out, duplicates


def verify_migration(original: dict, restored: dict) -> dict:
    """Compare ``original`` and ``restored`` project dicts for count and content.

    Order-insensitive (compares by id). Returns
    ``{"match": bool, "issues": [str], "milestone_count": int, "entry_count": int}``.
    """
    issues: list[str] = []

    # --- non-milestone meta -------------------------------------------------
    # schema_version is stamped by the store; ignore it on both sides so its
    # presence/absence is not reported as a content change.
    def meta_of(d: dict) -> dict:
        return {k: v for k, v in d.items()
                if k not in ("milestones", "schema_version")}

    o_meta, r_meta = meta_of(original), meta_of(restored)
    for key in set(o_meta) | set(r_meta):
        if o_meta.get(key) != r_meta.get(key):
            issues.append(f"meta field '{key}' differs")

    # --- milestones (by id) -------------------------------------------------
    # Duplicate milestone ids collapse in the dict below (and in the SQLite
    # (pk, sk) key), so a project carrying dup ids would silently lose a
    # milestone on migration and still verify clean. Detect and refuse it here
    # so migrate-on-first-use aborts rather than dropping data (the operator
    # recovers on the JSON backend via `beacon milestone purge`).
    o_ms_ids = [m.get("id") for m in original.get("milestones", []) or []]
    o_dup_ms = sorted({mid for mid in o_ms_ids
                       if mid and o_ms_ids.count(mid) > 1})
    if o_dup_ms:
        issues.append(f"duplicate milestone ids {o_dup_ms} "
                      "(cannot migrate faithfully to SQLite)")

    o_ms = {m.get("id"): m for m in original.get("milestones", []) or []}
    r_ms = {m.get("id"): m for m in restored.get("milestones", []) or []}
    missing = set(o_ms) - set(r_ms)
    extra = set(r_ms) - set(o_ms)
    if missing:
        issues.append(f"milestones missing after migration: {sorted(missing)}")
    if extra:
        issues.append(f"unexpected milestones after migration: {sorted(extra)}")

    # entry_count is the total in the ORIGINAL across ALL milestones, not just
    # the ones that survived — otherwise a lost milestone would shrink the count
    # and understate the loss exactly when it is worst.
    entry_count = 0
    for m in original.get("milestones", []) or []:
        o_idx, _ = _entry_index(m.get("entries", []) or [])
        entry_count += len(o_idx)

    for ms_id in sorted(set(o_ms) & set(r_ms)):
        o_entries, o_dups = _entry_index(o_ms[ms_id].get("entries", []) or [])
        r_entries, r_dups = _entry_index(r_ms[ms_id].get("entries", []) or [])
        for dup in sorted(set(o_dups) | set(r_dups)):
            issues.append(f"{ms_id}: duplicate entry id '{dup}' "
                          "(cannot verify faithfully)")
        e_missing = set(o_entries) - set(r_entries)
        e_extra = set(r_entries) - set(o_entries)
        if e_missing:
            issues.append(f"{ms_id}: entries missing: {sorted(e_missing)}")
        if e_extra:
            issues.append(f"{ms_id}: unexpected entries: {sorted(e_extra)}")
        for eid in sorted(set(o_entries) & set(r_entries)):
            # Compare the entry minus its own children list (children are
            # compared separately via the recursive index) so ordering of a
            # parent's child array does not cause a false diff.
            o_e = {k: v for k, v in o_entries[eid].items() if k != "entries"}
            r_e = {k: v for k, v in r_entries[eid].items() if k != "entries"}
            if o_e != r_e:
                issues.append(f"{ms_id}: entry '{eid}' content differs")

        # milestone metadata (minus entries) must also match.
        o_ms_meta = {k: v for k, v in o_ms[ms_id].items() if k != "entries"}
        r_ms_meta = {k: v for k, v in r_ms[ms_id].items() if k != "entries"}
        if o_ms_meta != r_ms_meta:
            issues.append(f"{ms_id}: milestone metadata differs")

    return {
        "match": not issues,
        "issues": issues,
        "milestone_count": len(o_ms),
        "entry_count": entry_count,
    }
