"""Cross-platform tool-record hook — the ``beacon-hook-save`` console script.

This is the entry point ``commands._resolve_hook_command`` prefers FIRST when it
writes ``settings.json`` (pipx / wheel / Windows installs), so it is the leg most
users actually run.

It used to be an independent translation of the old bash hook, carrying its own
``_MONITORED_SOURCES`` table that named two Google-Drive servers and skipped
everything else. ms-180 replaced that hand-written ``case`` with a tool-ledger
lookup in the BASH leg only — leaving this one, the preferred one, on the old
behaviour. A declared record sink therefore produced no record and no debt on a
normal install, and nothing reported it: exactly the "wiring exists but nothing
runs" defect ms-180 was written to remove, reproduced inside its own fix. An
independent philosophy review of PR #802 caught it.

So the decision no longer lives here. It lives once, in
``lib/tool_record_hook.handle``, and both legs are thin wrappers around it.

Fails safe: every path returns 0.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional


def _find_root(start: Path) -> Optional[Path]:
    """Walk up for a ``.beacon/project.json`` marker.

    The previous version checked only ``./.beacon/project.json`` and gave up —
    which silently did nothing whenever Claude Code's cwd was a subdirectory.
    Walking up matches every other Beacon hook (``bin/hook_bootstrap``).
    """
    try:
        cur = start.resolve()
    except Exception:
        return None
    for cand in (cur, *cur.parents):
        try:
            if (cand / ".beacon" / "project.json").exists():
                return cand
        except Exception:
            continue
    return None


def _import_shared():
    """Import ``lib/tool_record_hook`` — the single definition of the decision.

    Candidate layouts, in order: the installed wheel (``lib/`` is remapped into
    ``beacon_cli/_bundled_lib`` by ``pyproject.toml``'s package-dir), then the
    source tree. ``_bundled_lib`` is listed FIRST and explicitly, because this
    module's whole purpose is to work on the wheel install — resolving only the
    source layout is how the old hook ended up effectively unreachable there.
    """
    here = Path(__file__).resolve().parent            # beacon_cli/hooks
    for lib_dir in (here.parent / "_bundled_lib",      # wheel / pipx
                    here.parent.parent / "lib",        # source checkout
                    here.parent / "lib"):
        try:
            if not (lib_dir / "tool_record_hook.py").exists():
                continue
        except Exception:
            continue
        if str(lib_dir) not in sys.path:
            sys.path.insert(0, str(lib_dir))
        try:
            import tool_record_hook  # type: ignore[import-not-found]
            return tool_record_hook
        except Exception:
            return None
    return None


def main(argv: Optional[list] = None) -> int:
    try:
        raw = sys.stdin.read()
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return 0
        if not isinstance(payload, dict):
            return 0

        root = _find_root(Path(payload.get("cwd") or "."))
        if root is None:
            return 0

        shared = _import_shared()
        if shared is None:
            return 0  # fail-safe: cannot reach the shared decision → record nothing
        shared.handle(payload, root)
        return 0
    except Exception:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
