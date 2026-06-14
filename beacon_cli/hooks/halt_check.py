"""PostToolUse hook: surface STOP signals to the AI (ms-55 e-1721).

Companion to ``post_commit.py`` (commit detection) and ``save_hook.py``
(MCP auto-save). This hook closes the loop on the "止まる側" half of
ms-55 SPEC: after every tool call, check whether the inbox-side hook
has dropped a ``halt-request.json`` on disk for this session; if so,
surface the stop record to the AI as ``additionalContext`` so it can
finish the in-flight tool call cleanly and then halt.

Wiring (per-user, ``~/.claude/settings.json``)::

  {
    "hooks": {
      "PostToolUse": [
        {
          "command": ["beacon-hook-halt-check"],
          "matcher": "Bash"
        }
      ]
    }
  }

Or, for Bash + any tool::

  "matcher": "*"

Failure modes
-------------
Like all Beacon hooks, this script swallows every error and returns 0 —
a misbehaving halt-check MUST NOT block the user's tool call. If the
halt request is malformed, missing, or already acknowledged, the hook
exits silently.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional


def _find_beacon_root(start: Path) -> Optional[Path]:
    """Walk up from `start` looking for a ``.beacon/project.json`` marker."""
    cur = start.resolve()
    while True:
        if (cur / ".beacon" / "project.json").exists():
            return cur
        if cur == cur.parent:
            return None
        cur = cur.parent


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _resolve_session_id(root: Path, hook_input: dict) -> str:
    """Same precedence as the bus inbox hook: session.json first,
    harness's view second."""
    sess = _read_json(root / ".beacon" / "session.json")
    return (
        sess.get("session_id")
        or hook_input.get("session_id")
        or ""
    )


def _import_stop_signal():
    """Lazy import of lib/stop_signal from the source tree.

    Mirrors the resolution in bin/beacon-bus-inbox-hook.py so both
    halves of the receive path use the same canonical module.
    """
    here = Path(__file__).resolve().parent
    # beacon_cli/hooks/ → parent of parent of parent = repo root, then lib/.
    candidates = [
        here.parent.parent.parent / "lib",
        here.parent.parent / "lib",
        here.parent / "lib",
    ]
    for lib_dir in candidates:
        candidate = lib_dir / "stop_signal.py"
        if candidate.exists():
            sys.path.insert(0, str(lib_dir))
            try:
                import stop_signal as _stop  # type: ignore[import-not-found]
            except Exception:
                return None
            return _stop
    return None


def _emit_inject(message: str) -> None:
    """Same JSON shape as the other Beacon PostToolUse hooks."""
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": message,
        }
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))


def main(argv: Optional[list] = None) -> int:
    """Entry point. Returns 0 always."""
    try:
        raw = sys.stdin.read()
        try:
            hook_input = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return 0

        cwd = Path(hook_input.get("cwd") or os.getcwd())
        root = _find_beacon_root(cwd)
        if root is None:
            return 0
        session_id = _resolve_session_id(root, hook_input)
        if not session_id:
            return 0
        _stop = _import_stop_signal()
        if _stop is None:
            return 0

        beacon_dir = str(root / ".beacon")
        request = _stop.read_halt_request(
            session_id, beacon_dir=beacon_dir,
        )
        if not _stop.halt_inject_needed(request):
            return 0

        message = _stop.render_halt_inject(request)
        if not message:
            return 0
        _emit_inject(message)

        # Stamp acknowledged_at so repeated PostToolUse fires don't
        # surface the same halt over and over. The AI has now been told;
        # if it doesn't halt, that's a higher-layer concern (the user
        # explicit override is still possible).
        try:
            _stop.acknowledge_halt_request(
                session_id, beacon_dir=beacon_dir,
                note="surfaced via PostToolUse",
            )
        except Exception:
            # Failing to ack is annoying (= re-surface next call) but
            # not catastrophic; don't break the inject.
            pass
        return 0
    except Exception:
        # Hook must never propagate exceptions to Claude Code.
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
