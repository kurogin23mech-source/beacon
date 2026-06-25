"""Codex stable session identity (= ms-93 / e-2497).

Codex CLI lacks the Claude Code bridge (channel/bus.mjs) that keeps the
session_id stable via bridge claim + heartbeat. Without it, every CLI
call from a long-running Codex run falls through to
``lib/session.get_or_mint_session()``'s freshness path and mints a new
sid whenever ``last_active`` is older than 3600s (= 2026-06-25 Codex
dogfood observation: same Codex process, 1h32m gap, sid rotated from
``...-6e2fa00b`` to ``...-5e5340e7``).

This module is the storage layer for the Codex side equivalent of
``bridges/<sid>.json``. The receive loop daemon (= scripts/codex-
receive-loop.py) is the *consumer*: it reads / mints sid at cold
start, sets ``BEACON_BUS_SENDER`` in its env so all CLI calls inside
the loop use the same sid, heartbeats periodically, and stamps
``closed=true`` on graceful shutdown.

Per Codex 2026-06-25 design feedback we DO NOT use a single
``~/.beacon/codex_session.json`` because that collapses
multi-project / multi-Codex parallel runs onto one sid. Instead:

    ~/.beacon/codex/sessions/<project_id>/<stable_instance_key>.json

where ``stable_instance_key`` is derived from cwd + parent_pid so the
same physical Codex run (= same cwd, same parent Codex pid) finds its
own sid even after a CLI bounce.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path


CODEX_SESSIONS_DIR_REL = Path(".beacon") / "codex" / "sessions"


@dataclass
class CodexSession:
    """On-disk record for a Codex stable session.

    Schema (per Codex 2026-06-25 design feedback): besides the
    identity tuple (project_id, cwd, agent_kind, pid, parent_pid),
    we keep ``beacon_bin`` so the receive loop and downstream CLI
    invocations agree on which beacon to use. ``closed`` / ``last_seen``
    are stamped on graceful shutdown and consulted by post-mortem
    inspection — files are not deleted on shutdown so operators can
    audit what happened.
    """

    session_id: str
    project_id: str
    cwd: str
    pid: int  # the receive-loop pid (= bridge equivalent)
    parent_pid: int  # the Codex process that spawned the loop
    beacon_bin: str = ""
    agent_kind: str = "codex"
    created_at: str = ""
    last_active: str = ""
    closed: bool = False
    last_seen: str = ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _user_codex_root(home: str = "") -> Path:
    """Return ``<home>/.beacon/codex`` — the Codex-side identity root.

    Uses ``$HOME`` by default; tests inject a ``home`` arg so they can
    keep state on tmp_path without touching the user's real ~/.beacon/.
    """
    base = Path(home or os.path.expanduser("~"))
    return base / ".beacon" / "codex"


def codex_session_path(project_id: str, stable_instance_key: str, home: str = "") -> Path:
    """Compute the on-disk path for a Codex session record.

    Layout (= Codex 2026-06-25 design feedback): we shard by
    project_id then by stable_instance_key so two Codex runs in the
    same project from different cwds get their own sid, and parallel
    Codex processes in the same cwd (rare but possible) also stay
    separate.
    """
    return _user_codex_root(home) / "sessions" / project_id / f"{stable_instance_key}.json"


def derive_stable_instance_key(cwd: str, parent_pid: int) -> str:
    """Build a key that is stable across CLI bounces within one Codex run.

    Codex feedback: "cwd hash + parent_pid is realistic until Codex
    exposes a session id we can rely on". A 16-hex-char prefix of
    sha256(cwd) is enough to disambiguate across normal user
    directories without producing absurdly long file names.
    """
    cwd_hash = hashlib.sha256(cwd.encode("utf-8")).hexdigest()[:16]
    return f"{cwd_hash}-{parent_pid}"


def _mint_codex_session_id() -> str:
    """``codex-<epoch_ms>-<8 hex>`` — distinct shape from Claude Code mint.

    The shape lets the server-side directory filter (= AC #7) tell
    Codex sessions apart from Claude Code / bclaude sessions without
    having to peek at ``actor.agent_kind``.
    """
    epoch_ms = int(time.time() * 1000)
    nonce = secrets.token_hex(4)
    return f"codex-{epoch_ms}-{nonce}"


def read_codex_session(path: Path) -> CodexSession | None:
    """Load a session record, or return ``None`` when the file is absent
    or unreadable (= we treat any IO failure as "no record" so the
    caller mints a fresh one)."""
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or "session_id" not in data:
        return None
    return CodexSession(
        session_id=data["session_id"],
        project_id=data.get("project_id", ""),
        cwd=data.get("cwd", ""),
        pid=int(data.get("pid", 0) or 0),
        parent_pid=int(data.get("parent_pid", 0) or 0),
        beacon_bin=data.get("beacon_bin", ""),
        agent_kind=data.get("agent_kind", "codex"),
        created_at=data.get("created_at", ""),
        last_active=data.get("last_active", ""),
        closed=bool(data.get("closed", False)),
        last_seen=data.get("last_seen", ""),
    )


def write_codex_session(path: Path, session: CodexSession) -> None:
    """Persist a session record (creates parent dirs)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(session)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def acquire_codex_session(
    *,
    project_id: str,
    cwd: str,
    parent_pid: int,
    pid: int,
    beacon_bin: str = "",
    home: str = "",
) -> CodexSession:
    """Cold-start: reuse the existing session if it matches the
    instance key and is not closed; otherwise mint a fresh one.

    A previously-closed file is *replaced* with a fresh sid: leaving
    the closed record in place would let the next loop adopt a sid the
    server already knows is gone. Operators who want to inspect the
    closed session can read it from ``<path>.closed`` (= the rename
    we do before minting fresh).
    """
    key = derive_stable_instance_key(cwd, parent_pid)
    path = codex_session_path(project_id, key, home=home)
    now = _now_iso()

    existing = read_codex_session(path)
    if existing is not None and not existing.closed:
        # Reuse. Update mutable fields (pid may change across bounces).
        existing.pid = pid
        existing.last_active = now
        if beacon_bin:
            existing.beacon_bin = beacon_bin
        write_codex_session(path, existing)
        return existing

    if existing is not None and existing.closed:
        # Preserve the closed record for post-mortem before reusing the path.
        archive = path.with_suffix(".json.closed")
        try:
            path.rename(archive)
        except OSError:
            pass  # Best-effort; if rename fails we still mint fresh.

    session = CodexSession(
        session_id=_mint_codex_session_id(),
        project_id=project_id,
        cwd=cwd,
        pid=pid,
        parent_pid=parent_pid,
        beacon_bin=beacon_bin,
        agent_kind="codex",
        created_at=now,
        last_active=now,
        closed=False,
        last_seen="",
    )
    write_codex_session(path, session)
    return session


def heartbeat_codex_session(path: Path) -> None:
    """Stamp ``last_active`` on an existing record (= called from the
    receive loop's 30s heartbeat tick). No-op when the record is
    missing; the caller's next acquire pass will recreate it."""
    existing = read_codex_session(path)
    if existing is None or existing.closed:
        return
    existing.last_active = _now_iso()
    write_codex_session(path, existing)


def close_codex_session(path: Path, *, reason: str = "shutdown") -> None:
    """Graceful shutdown: stamp ``closed=true`` + ``last_seen`` so the
    file remains for post-mortem inspection. Do NOT delete — Codex
    feedback explicitly preferred this for diagnostic clarity."""
    existing = read_codex_session(path)
    if existing is None:
        return
    existing.closed = True
    existing.last_seen = _now_iso()
    write_codex_session(path, existing)


__all__ = [
    "CodexSession",
    "acquire_codex_session",
    "close_codex_session",
    "codex_session_path",
    "derive_stable_instance_key",
    "heartbeat_codex_session",
    "read_codex_session",
    "write_codex_session",
]
