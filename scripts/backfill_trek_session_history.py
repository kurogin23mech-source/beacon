#!/usr/bin/env python3
"""One-shot backfill of ``trek.session_history`` for existing Trek docs.

ms-86 / e-2225 AC3. Walks every Trek file under ``~/.beacon/treks/`` (or
the path pointed at by ``BEACON_TREKS_DIR``) and, for each doc that does
not already carry a populated ``session_history`` list, derives one from
the three pre-existing session_id locations:

  1. ``leader_session_id`` — the current leader's session
  2. ``halt.issued_by_session_id`` — the session that pulled the Andon
     cord (may be an external session not present in members[])
  3. ``task_states[*].updated_by_session_id`` — every session that has
     stamped a task state at least once

Each derived entry follows the e-2225 schema:

  {
    "session_id": str,
    "user_id": str,
    "email": str,
    "joined_at": ISO timestamp,
    "role_at_join": "leader" | "member",
  }

Default mode is **dry-run** (= prints what would change, writes nothing).
Pass ``--apply`` to actually persist the updates. The script is
idempotent — re-running it on already-backfilled Treks is a no-op.

Usage::

    # Inspect what would change (default, no writes):
    python3 scripts/backfill_trek_session_history.py

    # Apply the writes:
    python3 scripts/backfill_trek_session_history.py --apply

    # Point at a non-default treks dir (e.g. test fixture):
    BEACON_TREKS_DIR=/tmp/treks python3 scripts/backfill_trek_session_history.py

The cloud-mode counterpart (= treks stored in Firestore / DynamoDB) is
out of scope here; this script targets only local-mode files. Cloud-mode
docs self-heal at next ``GET`` because ``server/firestore_client`` reads
through the same trek_store path during the local-mode tests, and the
production read paths can call ``trek.backfill_session_history`` directly
on the loaded doc when needed (= follow-up task if cloud regression
surfaces).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Allow running from project root: ``python3 scripts/backfill_trek_...``
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from lib import trek as trek_mod  # noqa: E402
from lib import trek_store  # noqa: E402


def _format_entry(e: dict) -> str:
    return (
        f"      session_id={e.get('session_id', '')}  "
        f"role_at_join={e.get('role_at_join', '')}  "
        f"user_id={e.get('user_id', '')}  "
        f"email={e.get('email', '')}  "
        f"joined_at={e.get('joined_at', '')}"
    )


def _process_dir(treks_dir: Path, *, apply: bool) -> tuple[int, int, int]:
    """Walk ``treks_dir`` and (optionally) write backfilled session_history.

    Returns ``(total, updated, skipped)``:
      total   — number of trek files inspected
      updated — number of files that gained at least one history entry
      skipped — files already carrying a populated session_history list
    """
    if not treks_dir.exists():
        print(f"[backfill] dir not found: {treks_dir} — nothing to do")
        return 0, 0, 0
    total = 0
    updated = 0
    skipped = 0
    for path in sorted(treks_dir.glob("*.json")):
        total += 1
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
            doc = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[backfill] SKIP unreadable {path.name}: {exc}",
                  file=sys.stderr)
            continue
        existing = doc.get("session_history") or []
        if existing:
            # Already populated by an earlier run or new_trek() — don't
            # touch. The lazy hydration path will still add entries for
            # newly-seen session ids on future joins; we're only here for
            # the legacy gap.
            skipped += 1
            continue
        # Derive new entries. The helper appends in-place; capture before /
        # after lengths to detect the no-op case where the doc had no
        # session_id anywhere (= a freshly-created planning trek with no
        # leader session id is rare but possible).
        added = trek_mod.backfill_session_history(doc)
        if added <= 0:
            skipped += 1
            continue
        print(f"[backfill] {path.name}: +{added} entr"
              f"{'y' if added == 1 else 'ies'}")
        for e in doc.get("session_history", []):
            print(_format_entry(e))
        if apply:
            try:
                # Use trek_store.save_trek to honour the canonical write
                # path (= same indent / encoding settings as a normal save).
                # We bypass load_trek's lazy-hydration this way; the doc
                # we have in memory is the post-hydration state.
                trek_store.save_trek(doc)
                updated += 1
            except (OSError, ValueError) as exc:
                print(f"[backfill] FAILED to write {path.name}: {exc}",
                      file=sys.stderr)
        else:
            updated += 1
    return total, updated, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill trek.session_history from legacy session_id "
            "locations. Default = dry-run; pass --apply to write."
        ),
    )
    parser.add_argument(
        "--apply", action="store_true",
        help=(
            "Persist the derived session_history entries to disk. Without "
            "this flag the script only prints what would change."
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
    print(f"[backfill] treks dir: {treks_dir}")
    print(f"[backfill] mode: {'APPLY' if args.apply else 'DRY-RUN'}")
    total, updated, skipped = _process_dir(treks_dir, apply=args.apply)
    print(
        f"[backfill] done: total={total} updated={updated} skipped={skipped}"
    )
    if not args.apply and updated > 0:
        print("[backfill] re-run with --apply to persist the writes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
