#!/usr/bin/env python3
"""Migrate ``trek.members[]`` from user_id keyed to session_id keyed (ms-97 / e-2658).

AC6 cutover の前提となる "1 trek doc に対する 1 回限りの破壊的書き換え" を
原子的に行う script。 user_id keyed (= 1 entry / user_id) を session_id keyed
(= 1 entry / session_id) に展開し、 元 entries は ``members_legacy_backup``
に snapshot として残す (= reverse script で復元可能)。

展開ルール:
- 各 user_id keyed member entry について、 ``session_history[]`` の中で
  ``user_id`` が一致する entry を全て探し、 1 session = 1 member entry に
  expand する。
- ``role`` 解決: 旧 entry の role が "leader" の時は、 ``leader_session_id``
  に一致する session のみ "leader" を維持し、 他 session は "member" に
  落とす (= 1 trek に 1 leader session という不変条件を維持)。
- session_history に存在しない user_id は orphan member として扱い、
  warning だけ出して skip する (= 完全な破壊を避ける、 後続で手当て可能)。

実行後の trek_doc:
- ``members[]``: session_id keyed
- ``members_legacy_backup[]``: 旧 user_id keyed snapshot
- ``meta.migration_phase``: ``"A"``

Idempotency:
- ``meta.migration_phase`` が既に ``"A"`` / ``"B"`` / ``"C"`` の trek は
  ``ValueError`` で拒否 (= 二重 migrate の不変条件破壊を防ぐ)。

Usage::

    # Dry-run (= 何も書き戻さず差分を表示):
    python3 scripts/migrate_members_session_keyed.py tk-1234abcd --dry-run

    # 実適用 (= beacon API 経由で trek_doc を書き戻す):
    python3 scripts/migrate_members_session_keyed.py tk-1234abcd \\
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


def migrate_trek(trek_doc: dict) -> dict:
    """Mutate ``trek_doc`` in place: members[] → session_id keyed.

    Pure mutator (= no I/O). Callers persist via ``db.save_trek`` or
    equivalent backend hook.

    Returns the same ``trek_doc`` (= chain-friendly).

    Raises:
        ValueError: if the trek is already past pre-A phase (= refuse
            double migration to keep the inverse rollback unambiguous).
    """
    phase = trek_mod.get_migration_phase(trek_doc)
    if phase != "pre-A":
        raise ValueError(
            f"migrate_trek: trek already at phase {phase!r}; "
            f"refusing double migration (= run revert script first)"
        )

    # 1) Snapshot original members[] before mutation (= the rollback
    #    contract). ``backup_legacy_members`` raises if the backup field
    #    is already populated, which gives us a second-line guard against
    #    silent re-runs.
    trek_mod.backup_legacy_members(trek_doc)

    legacy_members = trek_doc.get("members_legacy_backup") or []
    session_history = trek_doc.get("session_history") or []
    leader_sid = trek_doc.get("leader_session_id") or ""

    # Build a per-user_id list of session_history entries so we can
    # expand each legacy member to N session-grain entries deterministically.
    sessions_by_user: dict[str, list[dict]] = {}
    for h in session_history:
        uid = h.get("user_id") or ""
        if not uid:
            continue
        sessions_by_user.setdefault(uid, []).append(h)

    new_members: list[dict] = []
    for legacy in legacy_members:
        user_id = legacy.get("user_id") or ""
        role = legacy.get("role") or "member"
        email = legacy.get("email") or ""
        invited_at = legacy.get("invited_at") or ""
        invited_by = legacy.get("invited_by") or ""
        sessions = sessions_by_user.get(user_id) or []
        if not sessions:
            # No session_history entry for this user_id → orphan. Skip
            # but emit a structured note so callers can audit.
            print(
                f"  warn: user_id={user_id!r} has no session_history entry; "
                f"skipping (= orphan member)",
                file=sys.stderr,
            )
            continue
        for h in sessions:
            sid = h.get("session_id") or ""
            if not sid:
                continue
            # Leader role collapses to a single session: only the one
            # stamped as ``leader_session_id`` keeps the leader role.
            # Every other session of the same user_id falls back to
            # "member" so the 1-leader invariant survives the expansion.
            entry_role = role
            if role == "leader" and sid != leader_sid:
                entry_role = "member"
            new_members.append({
                "session_id": sid,
                "user_id": user_id,
                "email": email or h.get("email") or "",
                "role": entry_role,
                "joined_at": h.get("joined_at") or "",
                "invited_at": invited_at,
                "invited_by": invited_by,
            })

    trek_doc["members"] = new_members
    trek_mod.set_migration_phase(trek_doc, "A")
    return trek_doc


def _load_trek_local(trek_id: str, project_id: Optional[str]) -> dict:
    """Best-effort load via lib.trek_store (= local-mode file backend).

    Cloud-mode trek_doc は server API 経由でしか拾えないため、 そのケースは
    呼び出し側で別経路を用意する (= --project は forward-compat な hook で、
    現状は local-mode trek_store のみ対応)。
    """
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
            "Migrate trek.members[] from user_id keyed to session_id keyed "
            "(ms-97 / e-2658 AC6 cutover)."
        )
    )
    parser.add_argument("trek_id", help="Target trek id (= tk-XXXXXXXX)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the post-migration members[] diff but write nothing.",
    )
    parser.add_argument(
        "--project", default=None,
        help=(
            "Optional project id hint (= forward-compat for cloud-mode "
            "trek storage; currently unused, local-mode trek_store path "
            "is used)."
        ),
    )
    args = parser.parse_args(argv)

    trek_doc = _load_trek_local(args.trek_id, args.project)
    before_members = list(trek_doc.get("members") or [])
    try:
        migrate_trek(trek_doc)
    except ValueError as exc:
        print(f"migration refused: {exc}", file=sys.stderr)
        return 2

    after_members = trek_doc.get("members") or []
    print(f"trek_id: {args.trek_id}")
    print(f"  before: {len(before_members)} member entries (user_id keyed)")
    print(f"  after:  {len(after_members)} member entries (session_id keyed)")
    print(f"  meta.migration_phase: {trek_doc['meta']['migration_phase']}")
    print(json.dumps(after_members, indent=2, sort_keys=True))

    if args.dry_run:
        print("--dry-run: nothing written")
        return 0

    _save_trek_local(args.trek_id, trek_doc)
    print("applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
