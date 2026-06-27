#!/usr/bin/env python3
"""Stamp ``meta.legacy_project_wide_scope = True`` on grandfathered treks.

ms-97 / e-2571 AC8 + AC31. Walks every Trek file under ``~/.beacon/treks``
(or the path pointed at by ``BEACON_TREKS_DIR``) and, for each trek whose
``scope[]`` contains a project-wide entry (= no narrowing key), sets
``meta.legacy_project_wide_scope = True``. The marker tells CLI / Web UI
that the trek is grandfathered — its scope is preserved as-is, but the
warning surface (= ``beacon trek list --legacy-scope``) can list it
without re-walking scope on every read.

Stamping does NOT touch ``scope[]``. Existing executors keep their
reach; the marker is purely a read-side hint.

Default mode is **dry-run** (= prints what would change, writes nothing).
Pass ``--apply`` to actually persist, or ``--rollback --apply`` to remove
the marker (e.g. when an operator narrows the grandfathered entries).

Usage::

    # Inspect what would change:
    python3 scripts/stamp_legacy_project_wide_scope.py

    # Apply the marker:
    python3 scripts/stamp_legacy_project_wide_scope.py --apply

    # Remove the marker (rollback):
    python3 scripts/stamp_legacy_project_wide_scope.py --rollback --apply

    # Point at a non-default treks dir:
    BEACON_TREKS_DIR=/tmp/treks python3 \\
        scripts/stamp_legacy_project_wide_scope.py

Cloud-mode docs (= Firestore / DynamoDB) are out of scope for this
script; the cutover plan lives in the SPEC's Migration リスク section.
The same lib helper (``stamp_legacy_project_wide_scope``) is intended to
run inside ``server/firestore_client`` at upgrade time.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from lib import trek as trek_mod  # noqa: E402
from lib import trek_store  # noqa: E402


def _format_scope(scope: list[dict]) -> str:
    parts = []
    for s in scope:
        narrowing = ",".join(
            f"{k}={s[k]}" for k in ("milestone", "operation", "task") if s.get(k)
        )
        marker = f" ({narrowing})" if narrowing else " [project-wide]"
        parts.append(f"{s.get('project', '?')}{marker}")
    return "; ".join(parts) or "<empty>"


def _process_dir(
    treks_dir: Path, *, apply: bool, rollback: bool,
) -> tuple[int, int, int]:
    """Walk ``treks_dir`` and (optionally) stamp / un-stamp the marker.

    Returns ``(total, updated, skipped)``.
    """
    if not treks_dir.exists():
        print(
            f"[stamp-legacy-scope] dir not found: {treks_dir} — nothing to do"
        )
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
                f"[stamp-legacy-scope] SKIP unreadable {path.name}: {exc}",
                file=sys.stderr,
            )
            continue
        if rollback:
            changed = trek_mod.unstamp_legacy_project_wide_scope(doc)
        else:
            changed = trek_mod.stamp_legacy_project_wide_scope(doc)
        if not changed:
            skipped += 1
            continue
        action = "UNSTAMP" if rollback else "STAMP"
        print(f"[stamp-legacy-scope] {action} {path.name}")
        print(f"      trek_id={doc.get('trek_id', '?')}  "
              f"title={doc.get('title', '')[:60]}")
        print(f"      scope:  {_format_scope(doc.get('scope') or [])}")
        if apply:
            try:
                trek_store.save_trek(doc)
                updated += 1
            except (OSError, ValueError) as exc:
                print(
                    f"[stamp-legacy-scope] FAILED to write {path.name}: {exc}",
                    file=sys.stderr,
                )
        else:
            updated += 1
    return total, updated, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Stamp meta.legacy_project_wide_scope on grandfathered treks "
            "(ms-97 / e-2571 AC8 + AC31). Default = dry-run; pass --apply "
            "to write. Pass --rollback (with --apply) to remove the marker."
        ),
    )
    parser.add_argument(
        "--apply", action="store_true",
        help=(
            "Persist the marker. Without this flag the script only prints "
            "what would change."
        ),
    )
    parser.add_argument(
        "--rollback", action="store_true",
        help=(
            "Remove the marker instead of stamping. Useful when the "
            "operator narrows the grandfathered scope entries."
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
    print(f"[stamp-legacy-scope] treks dir: {treks_dir}")
    print(
        f"[stamp-legacy-scope] mode: "
        f"{'APPLY' if args.apply else 'DRY-RUN'} "
        f"{'(rollback)' if args.rollback else '(stamp)'}"
    )
    total, updated, skipped = _process_dir(
        treks_dir, apply=args.apply, rollback=args.rollback,
    )
    print(
        f"[stamp-legacy-scope] done: total={total} updated={updated} "
        f"skipped={skipped}"
    )
    if not args.apply and updated > 0:
        print(
            "[stamp-legacy-scope] re-run with --apply to persist the writes."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
