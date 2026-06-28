#!/usr/bin/env python3
"""Reverse of ``scripts/migrate_members_session_keyed.py`` (ms-97 / e-2658).

Phase A → pre-A の hot rollback。 ``trek.members_legacy_backup`` から
``members[]`` を復元し、 ``meta.migration_phase`` を ``"pre-A"`` に戻す。
backup が空 (= 元々 migration されていない / 既に revert 済み) の trek は
``ValueError`` で拒否する (= 復元元が無い)。

Usage::

    # Dry-run (= members[] diff のみ表示):
    python3 scripts/revert_members_session_keyed.py tk-1234abcd --dry-run

    # 実適用:
    python3 scripts/revert_members_session_keyed.py tk-1234abcd \\
        --project beacon-b95643
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from lib import trek as trek_mod  # noqa: E402


def revert_trek(trek_doc: dict) -> dict:
    """Mutate ``trek_doc`` in place: members[] ← members_legacy_backup.

    Returns the same ``trek_doc`` (= chain-friendly). Raises ``ValueError``
    if the backup field is empty (= nothing to restore, see
    ``trek.restore_legacy_members``).
    """
    trek_mod.restore_legacy_members(trek_doc)
    return trek_doc


def _load_trek_local(trek_id: str, project_id: Optional[str]) -> dict:
    from lib import trek_store  # noqa: E402

    doc = trek_store.load_trek(trek_id)
    if not doc:
        raise SystemExit(f"trek {trek_id} not found in local store")
    return doc


def _save_trek_local(trek_id: str, trek_doc: dict) -> None:
    from lib import trek_store  # noqa: E402

    trek_store.save_trek(trek_id, trek_doc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Revert trek.members[] from session_id keyed back to user_id "
            "keyed (ms-97 / e-2658 hot rollback)."
        )
    )
    parser.add_argument("trek_id", help="Target trek id (= tk-XXXXXXXX)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the restored members[] but write nothing.",
    )
    parser.add_argument(
        "--project", default=None,
        help="Forward-compat hint (= cloud-mode hook; unused today).",
    )
    args = parser.parse_args(argv)

    trek_doc = _load_trek_local(args.trek_id, args.project)
    try:
        revert_trek(trek_doc)
    except ValueError as exc:
        print(f"revert refused: {exc}", file=sys.stderr)
        return 2

    after_members = trek_doc.get("members") or []
    print(f"trek_id: {args.trek_id}")
    print(f"  restored: {len(after_members)} member entries (user_id keyed)")
    print(
        f"  meta.migration_phase: "
        f"{(trek_doc.get('meta') or {}).get('migration_phase')}"
    )
    print(json.dumps(after_members, indent=2, sort_keys=True))

    if args.dry_run:
        print("--dry-run: nothing written")
        return 0

    _save_trek_local(args.trek_id, trek_doc)
    print("applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
