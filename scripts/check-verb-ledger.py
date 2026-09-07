#!/usr/bin/env python3
"""Pre-commit / local gate: the Q/R/B/C verb ledger must cover the live CLI
surface (ms-114 e-6275).

WHY this exists. ``lib/verb_ledger.py`` classifies every live CLI verb into the
Q/R/B/C systems, and ``tests/test_verb_ledger.py::test_ledger_covers_live_surface``
already fails CI when a newly-added verb is missing from the ledger (or a stale
entry names a verb that no longer exists). But that only surfaces ~10 minutes into
the CI ``test`` job — the same forgotten-ledger-entry drift bit PR #734 (the
``dm_show`` verb) and PR #735 (the ``attention`` verb) in a row, each costing a
full red-CI round-trip. This script runs the SAME ``verb_ledger.reconcile()`` at
commit time so a forgotten entry is caught in seconds, locally, before push.

Contract (mirrors the other pre-commit gates, e.g. check-capability-scope.py):
  * exit 0  — ledger covers the live surface (or nothing to check).
  * exit 1  — DRIFT: unclassified live verbs and/or stale ledger entries. Prints
              which verbs so the author knows exactly what to add to
              ``lib/verb_ledger_data.py`` (or delete). This is the blocking case.
  * exit 0 + a ``[skip]`` note — the ledger module could not be imported/run
              (e.g. run outside the repo, or an unrelated import error). We do NOT
              block a commit on a tooling failure of the gate itself — the real
              guarantee is carried by the CI test; this script is the early, best-
              effort mirror. Bricking every commit on a gate bug is worse than the
              (bounded) miss of a gate that could not run.

Run standalone: ``python3 scripts/check-verb-ledger.py`` (from the repo root).
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    lib_dir = os.path.join(repo_root, "lib")
    sys.path.insert(0, lib_dir)

    try:
        import verb_ledger as vl  # type: ignore[import-not-found]
        rec = vl.reconcile()
    except Exception as exc:  # fail-safe: never block a commit on a gate bug
        print(f"[skip] verb ledger gate unavailable ({exc}); "
              f"CI test_verb_ledger.py still enforces coverage.")
        return 0

    unclassified = rec.get("unclassified") or []
    stale = rec.get("stale") or []
    if not unclassified and not stale:
        return 0

    if unclassified:
        print("未分類の CLI verb があります (lib/verb_ledger_data.py の VERB_LEDGER に "
              "Q/R/B/C で追記してください):")
        for v in unclassified:
            print(f"  + {v}")
    if stale:
        print("存在しない verb が ledger に残っています (削除するか alias にしてください):")
        for v in stale:
            print(f"  - {v}")
    print("→ CI (tests/test_verb_ledger.py::test_ledger_covers_live_surface) を "
          "待たず、ここで解消できます。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
