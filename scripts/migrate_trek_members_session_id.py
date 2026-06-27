#!/usr/bin/env python3
"""Rewrite ``trek.members[]`` from user_id keyed to session_id keyed.

ms-97 / e-2566 AC6 + AC34. Walks every Trek file under ``~/.beacon/treks/``
(or the path pointed at by ``BEACON_TREKS_DIR``) and rewrites each member
entry so it carries the ``session_id`` field used by the post ms-97
codebase. The canonical session for each user is the **earliest** entry
in ``session_history`` (= the persistent join log added in ms-86 / e-2225)
that matches the member's user_id.

The original list is preserved verbatim under ``members_legacy_backup``
so ``--rollback`` can restore it. Migration is idempotent — re-running
on an already-migrated doc is a no-op.

Default mode is **dry-run** (= prints what would change, writes nothing).
Pass ``--apply`` to actually persist the updates, or ``--rollback`` (with
``--apply``) to restore from ``members_legacy_backup``.

Usage::

    # Inspect what would change (default, no writes):
    python3 scripts/migrate_trek_members_session_id.py

    # Apply the rewrites:
    python3 scripts/migrate_trek_members_session_id.py --apply

    # Roll the rewrites back (= restore from members_legacy_backup):
    python3 scripts/migrate_trek_members_session_id.py --rollback --apply

    # Point at a non-default treks dir (e.g. test fixture):
    BEACON_TREKS_DIR=/tmp/treks python3 \\
        scripts/migrate_trek_members_session_id.py

Members whose user_id has no matching session_history entry are surfaced
in stderr and left without ``session_id`` (= partial migration). Operators
are expected to inspect them and either backfill the history (= rerun the
e-2225 script first) or contact the user to confirm whether the member
should be removed.

Cloud-mode docs (= Firestore / DynamoDB) are out of scope for this
script. The cutover plan for cloud-mode treks lives in the SPEC's
Migration リスク section — Phase A applies the same lib helper to docs
read through ``server/firestore_client`` at upgrade time.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Allow running from project root: ``python3 scripts/migrate_trek_...``.
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from lib import trek as trek_mod  # noqa: E402
from lib import trek_store  # noqa: E402


def _format_member(m: dict) -> str:
    return (
        f"      session_id={m.get('session_id', '') or '<missing>'}  "
        f"user_id={m.get('user_id', '')}  "
        f"email={m.get('email', '')}  "
        f"role={m.get('role', '')}"
    )


def _migrate_doc(doc: dict) -> tuple[bool, list[str]]:
    """Migrate one trek doc in-place. Returns (changed, warnings)."""
    warnings: list[str] = []
    members_before = list(doc.get("members") or [])
    if not members_before:
        return False, warnings
    if trek_mod.members_are_session_id_keyed(doc):
        return False, warnings
    trek_mod.migrate_members_to_session_id_keyed(doc)
    members_after = doc.get("members") or []
    changed = members_after != members_before
    if changed:
        for m in members_after:
            if "session_id" not in m or not m.get("session_id"):
                warnings.append(
                    f"        ! partial: user_id={m.get('user_id', '')!r} "
                    "has no matching session_history entry — left without "
                    "session_id, inspect manually"
                )
    return changed, warnings


def _rollback_doc(doc: dict) -> bool:
    """Rollback one trek doc in-place. Returns whether anything changed."""
    backup = doc.get(trek_mod.MEMBERS_LEGACY_BACKUP_KEY)
    if not backup:
        return False
    trek_mod.rollback_members_migration(doc)
    return True


def _process_dir(
    treks_dir: Path, *, apply: bool, rollback: bool,
) -> tuple[int, int, int]:
    """Walk ``treks_dir`` and (optionally) write the migration.

    Returns ``(total, updated, skipped)``:
      total   — number of trek files inspected
      updated — number of files that gained a migration / rollback
      skipped — files already in the target schema (or empty members[])
    """
    if not treks_dir.exists():
        print(f"[migrate-members] dir not found: {treks_dir} — nothing to do")
        return 0, 0, 0
    total = 0
    updated = 0
    skipped = 0
    for path in sorted(treks_dir.glob("*.json")):
        total += 1
        try:
            with open(path, "r", encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            print(
                f"[migrate-members] SKIP unreadable {path.name}: {exc}",
                file=sys.stderr,
            )
            continue
        if rollback:
            changed = _rollback_doc(doc)
            warnings: list[str] = []
        else:
            changed, warnings = _migrate_doc(doc)
        if not changed:
            skipped += 1
            continue
        action = "ROLLBACK" if rollback else "MIGRATE"
        print(f"[migrate-members] {action} {path.name}")
        for m in doc.get("members", []):
            print(_format_member(m))
        for w in warnings:
            print(w, file=sys.stderr)
        if apply:
            try:
                trek_store.save_trek(doc)
                updated += 1
            except (OSError, ValueError) as exc:
                print(
                    f"[migrate-members] FAILED to write {path.name}: {exc}",
                    file=sys.stderr,
                )
        else:
            updated += 1
    return total, updated, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Rewrite trek.members[] from user_id keyed to session_id keyed "
            "(ms-97 / e-2566 AC6 + AC34). Default = dry-run; pass --apply "
            "to write. Pass --rollback (with --apply) to restore from "
            "members_legacy_backup."
        ),
    )
    parser.add_argument(
        "--apply", action="store_true",
        help=(
            "Persist the migration. Without this flag the script only "
            "prints what would change."
        ),
    )
    parser.add_argument(
        "--rollback", action="store_true",
        help=(
            "Restore members[] from members_legacy_backup instead of "
            "migrating. Useful when Phase A surfaces unexpected drift."
        ),
    )
    parser.add_argument(
        "--treks-dir", default="",
        help=(
            "Override the treks directory (= same shape as the "
            "BEACON_TREKS_DIR environment variable). When omitted, falls "
            "back to BEACON_TREKS_DIR or ~/.beacon/treks."
        ),
    )
    args = parser.parse_args(argv)
    if args.treks_dir:
        os.environ["BEACON_TREKS_DIR"] = args.treks_dir
    treks_dir = Path(trek_store.get_trek_dir())
    print(f"[migrate-members] treks dir: {treks_dir}")
    print(
        f"[migrate-members] mode: "
        f"{'APPLY' if args.apply else 'DRY-RUN'} "
        f"{'(rollback)' if args.rollback else '(migrate)'}"
    )
    total, updated, skipped = _process_dir(
        treks_dir, apply=args.apply, rollback=args.rollback,
    )
    print(
        f"[migrate-members] done: total={total} updated={updated} "
        f"skipped={skipped}"
    )
    if not args.apply and updated > 0:
        print(
            "[migrate-members] re-run with --apply to persist the writes."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
