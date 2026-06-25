#!/usr/bin/env python3
"""Standalone entry point for install Skills to resolve BEACON_BIN.

ms-93 / e-2276 phase 0: invoked from Codex / Claude Code install Skills
at startup. Wraps ``lib/beacon_bin_resolver.resolve_beacon_bin`` so the
Skill can pick the right beacon binary and the user-facing 1-line WARN
without re-implementing the resolution policy in markdown.

Usage:
    python3 scripts/beacon-bin-resolver.py [--install-root PATH] [--no-path-fallback]

Output:
    Single JSON line on stdout with the ``ResolutionResult`` shape (see
    ``lib/beacon_bin_resolver.py``). Exit code:
      0  verdict in {"ok", "soft_warn"}  — Skill may proceed
      1  verdict in {"hard_fail", "no-candidate"} — Skill must show advice and stop
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resolve BEACON_BIN + evaluate PATH health for install Skills.",
    )
    parser.add_argument(
        "--install-root",
        default="",
        help="Path to the beacon source checkout (= candidate <root>/bin/beacon).",
    )
    parser.add_argument(
        "--no-path-fallback",
        action="store_true",
        help="Do not fall through to bare `beacon` on PATH (= last resort).",
    )
    args = parser.parse_args()

    # The script and the helper module always live in the same beacon
    # source checkout, so the import path is __file__-relative — never
    # tied to ``--install-root`` (which is purely a candidate evaluation
    # input, see below).
    own_root = Path(__file__).resolve().parent.parent
    lib_dir = str(own_root / "lib")
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    import beacon_bin_resolver as resolver  # noqa: E402

    # ``--install-root`` controls where to look for a candidate
    # ``<root>/bin/beacon``. When the user doesn't supply it, fall back
    # to the script's own checkout so the typical Skill invocation
    # ("install Skill bundled with this beacon") finds the right bin.
    install_root = args.install_root or str(own_root)

    result = resolver.resolve_beacon_bin(
        env_bin=os.environ.get("BEACON_BIN", ""),
        install_root=install_root,
        allow_path_fallback=not args.no_path_fallback,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False))
    return 0 if result.verdict in ("ok", "soft_warn") else 1


if __name__ == "__main__":
    sys.exit(main())
