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
    """Backward-compat wrapper. Delegates to the shared pure mutator
    ``trek_mod.migrate_members_to_session_keyed`` (= same logic now lives
    in ``lib/trek.py`` so the server endpoint can reuse it).

    Returns the same ``trek_doc`` (= chain-friendly).

    Raises:
        ValueError: if the trek is already past pre-A phase.
    """
    before_phase = trek_mod.get_migration_phase(trek_doc)
    trek_mod.migrate_members_to_session_keyed(trek_doc)
    # Surface orphan warnings to stderr to preserve the legacy CLI UX
    # (= the previous inline implementation printed per-orphan lines).
    orphans = (trek_doc.get("meta") or {}).get("migration_orphans") or []
    for uid in orphans:
        print(
            f"  warn: user_id={uid!r} has no session_history entry; "
            f"skipping (= orphan member)",
            file=sys.stderr,
        )
    del before_phase  # consumed only for clarity above
    return trek_doc


def _load_trek_local(trek_id: str, project_id: Optional[str]) -> dict:
    """Best-effort load via lib.trek_store (= local-mode file backend)."""
    from lib import trek_store  # noqa: E402

    doc = trek_store.load_trek(trek_id)
    if not doc:
        raise SystemExit(f"trek {trek_id} not found in local store")
    return doc


def _save_trek_local(trek_id: str, trek_doc: dict) -> None:
    from lib import trek_store  # noqa: E402

    trek_store.save_trek(trek_id, trek_doc)


def _cloud_mode_available(project_id: Optional[str]) -> bool:
    """True iff ``--project`` was given AND ``.beacon/cloud.json`` exists.

    両条件揃った時のみ cloud endpoint 経由に振る (= cwd で local 開発中の
    operator が誤って prod API を叩かないようにする二重ガード)。
    """
    if not project_id:
        return False
    cloud_json = Path.cwd() / ".beacon" / "cloud.json"
    return cloud_json.exists()


def _migrate_via_cloud(
    trek_id: str, project_id: str, dry_run: bool,
) -> dict:
    """POST to the server migrate endpoint and return the response trek_doc.

    Uses ``lib.api_client.ApiClient`` (= same auth path as ``beacon cloud
    *`` CLI subcommands). Bearer token + ``X-Beacon-Session`` header are
    injected by ``ApiClient._request`` via the auth / session helpers.
    """
    sys.path.insert(0, str(ROOT / "lib"))
    from auth import load_credentials  # noqa: E402
    from api_client import ApiClient  # noqa: E402
    from commands import _resolve_active_api_url, _extract_token  # noqa: E402

    creds = load_credentials()
    if creds is None:
        raise SystemExit("Not logged in. Run: beacon auth login")
    api_url = _resolve_active_api_url()
    client = ApiClient(api_url, _extract_token(creds))
    path = (
        f"/api/treks/{trek_id}/migrate-members-session-keyed"
        f"?dry_run={'true' if dry_run else 'false'}"
    )
    return client.post(path)


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
            "Project id hint. When set AND .beacon/cloud.json exists, the "
            "migration is performed via the server endpoint "
            "(POST /api/treks/{trek_id}/migrate-members-session-keyed). "
            "Otherwise the local-mode trek_store path is used."
        ),
    )
    args = parser.parse_args(argv)

    if _cloud_mode_available(args.project):
        try:
            trek_doc = _migrate_via_cloud(
                args.trek_id, args.project, args.dry_run,
            )
        except RuntimeError as exc:
            # ApiClient raises RuntimeError("API error <code>: <detail>")
            # on 4xx/5xx; 409 = double-migration refusal.
            print(f"migration refused: {exc}", file=sys.stderr)
            return 2
        after_members = trek_doc.get("members") or []
        print(f"trek_id: {args.trek_id} (cloud-mode)")
        print(f"  after: {len(after_members)} member entries (session_id keyed)")
        print(f"  meta.migration_phase: {trek_doc.get('meta', {}).get('migration_phase')}")
        print(json.dumps(after_members, indent=2, sort_keys=True))
        print("--dry-run: server did not persist" if args.dry_run else "applied")
        return 0

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
