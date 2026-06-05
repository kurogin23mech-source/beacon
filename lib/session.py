"""Session identity for Beacon (ms-57 e-1035).

A "session" is one Claude Code (or bare CLI) run. We key per-session scratch
notes and the session log on its id, and pair it with a heartbeat so a live
session can be told apart from a dead one when rescuing orphaned notes.

Identity resolution:
  1. ``CLAUDE_CODE_SESSION_ID`` — Claude Code exports this into the environment
     of every CLI it spawns. It is stable for the whole session and globally
     unique (a UUID), so it survives across the many ``beacon`` invocations a
     single session makes. Preferred: there is no local state to manage.
  2. Minted fallback — for a bare CLI / CI run with no Claude Code env. We
     persist a marker at ``<root>/.beacon/session.json`` and reuse it while it
     exists, so sequential commands in the same shell share one id.

The cross-machine-safe key pairs this id with the actor
(``lib/agent.get_actor()`` — machine + optional agent name): two machines can
never collide because the actor differs; two sessions on one machine can never
collide because the id differs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

# Claude Code sets this for every spawned CLI (verified: ms-57 e-1035).
ENV_SESSION_ID = "CLAUDE_CODE_SESSION_ID"

_SESSION_JSON_RELATIVE = Path(".beacon") / "session.json"


def get_session_id(beacon_root: Optional[str] = None) -> str:
    """Return a stable id for the current session.

    Prefers ``CLAUDE_CODE_SESSION_ID``; otherwise returns (minting + persisting
    once) a per-project fallback id. Never raises — a session id is always
    returned so callers can key notes/log unconditionally.
    """
    env_id = os.environ.get(ENV_SESSION_ID, "")
    if env_id and env_id.strip():
        return env_id.strip()
    return _minted_session_id(beacon_root)


def _minted_session_id(beacon_root: Optional[str]) -> str:
    root = Path(beacon_root) if beacon_root else Path.cwd()
    marker = root / _SESSION_JSON_RELATIVE

    # Reuse an existing marker so commands within the same shell share an id.
    try:
        if marker.is_file():
            data = json.loads(marker.read_text(encoding="utf-8"))
            sid = data.get("id")
            if isinstance(sid, str) and sid:
                return sid
    except Exception:
        pass  # fall through to mint a fresh one

    sid = _mint(_actor_name())
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                {
                    "id": sid,
                    "actor": _actor_name(),
                    "created_at": _utcnow(),
                    "note": (
                        "Minted by beacon because CLAUDE_CODE_SESSION_ID was not "
                        "set. Keys this run's scratch notes / session log."
                    ),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass  # best-effort; a non-persisted id is still usable for this process
    return sid


def _mint(actor_name: str) -> str:
    import uuid

    return f"{actor_name}-{uuid.uuid4().hex[:12]}"


def _actor_name() -> str:
    """Best-effort actor label (lib/agent.get_actor), 'local' if unavailable."""
    try:
        import agent as _agent  # lib/ is on sys.path for both CLI and tests

        actor = _agent.get_actor() or {}
        return actor.get("agent") or actor.get("machine") or "local"
    except Exception:
        return "local"


def _utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
