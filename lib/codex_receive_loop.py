"""Codex receive-loop primitives (= ms-93 / e-2497, layer 2).

This module factors the receive-loop work into small, testable
functions so we can pin behaviour with mock API clients before
``scripts/codex-receive-loop.py`` runs anything for real. The actual
daemon is a thin shell around these primitives.

Responsibilities (mirroring Claude Code's channel/bus.mjs):

- ``heartbeat_to_server`` — PUT /api/projects/<pid>/sessions/<sid>
  keeps the server-side ``last_active`` fresh so the freshness
  threshold (3600s in lib/session.py) never expires our sid.
- ``poll_inbox_once`` — GET /api/projects/<pid>/bus/unread, filter to
  events addressed to us, persist them to the local inbox directory
  for the hook to pick up, ack delivered.
- ``ack_event`` — POST /api/projects/<pid>/bus/<event_id>/ack with
  stage ``delivered`` or ``opened`` (= mirrors bus.mjs's two-stage
  receipt logic).
- ``persist_inbox_event`` — write an event JSON to
  ``<cwd>/.beacon/codex/inbox/<event_id>.json`` (= where the Codex
  hook reads).

The daemon (= scripts/codex-receive-loop.py) wires these into a
``while True`` plus signal handlers; the loop body lives here so tests
can drive one iteration deterministically.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


INBOX_DIR_REL = Path(".beacon") / "codex" / "inbox"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ #
# Heartbeat
# ------------------------------------------------------------------ #


def heartbeat_to_server(
    api,
    *,
    project_id: str,
    session_id: str,
    actor: dict,
    poll_interval_ms: int = 2000,
    shutdown: bool = False,
) -> bool:
    """PUT /api/projects/<pid>/sessions/<sid> with current timestamp.

    Body shape mirrors channel/bus-heartbeat.mjs ``buildHeartbeatBody``
    (= ``last_active`` / ``last_poll_at`` / ``poll_interval_ms`` /
    ``shutdown``) so the server's poll-health computation (e-1318)
    treats the Codex receive loop the same way it treats the Claude
    Code bridge — the directory ``--healthy`` filter and the freshness
    threshold both kick in correctly.

    The ``actor`` payload is sent only on the first heartbeat (= mint
    path) or whenever the caller wants to refresh it; the field-path
    merge in server/firestore_client.py preserves the original
    actor.machine/agent so subsequent heartbeats can omit it safely.

    Returns ``True`` on success, ``False`` on transport failure
    (= we never raise; the loop must survive transient network blips).
    """
    now = _now_iso()
    body = {
        "last_active": now,
        "last_poll_at": now,
        "poll_interval_ms": poll_interval_ms,
        "shutdown": bool(shutdown),
    }
    if actor:
        body["actor"] = actor
    try:
        api.put(
            f"/api/projects/{project_id}/sessions/{session_id}",
            body,
        )
        return True
    except Exception:
        return False


# ------------------------------------------------------------------ #
# Inbox poll + persist + ack
# ------------------------------------------------------------------ #


def inbox_dir(cwd: str = "") -> Path:
    """Resolve ``<cwd>/.beacon/codex/inbox``, defaulting to ``Path.cwd()``."""
    return (Path(cwd) if cwd else Path.cwd()) / INBOX_DIR_REL


def persist_inbox_event(event: dict, cwd: str = "") -> Path | None:
    """Write ``event`` to the inbox directory as a per-event JSON file.

    Returns the path written, or ``None`` when the event has no
    ``event_id`` (= cannot be filed). The hook later reads any file
    found here and renders it into Codex's context (= UserPromptSubmit
    additionalContext).
    """
    event_id = (event or {}).get("event_id", "")
    if not event_id:
        return None
    target = inbox_dir(cwd) / f"{event_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as f:
        json.dump(event, f, ensure_ascii=False, indent=2)
    return target


def ack_event(api, *, project_id: str, event_id: str, stage: str,
              recipient_session_id: str) -> bool:
    """POST /api/projects/<pid>/bus/<event_id>/ack. Fire-and-forget."""
    if not event_id or stage not in ("delivered", "opened"):
        return False
    try:
        api.post(
            f"/api/projects/{project_id}/bus/{event_id}/ack",
            {"stage": stage, "recipient_session_id": recipient_session_id},
        )
        return True
    except Exception:
        return False


def _addressed_to_us(event: dict, our_sid: str) -> bool:
    """Drop events that are explicitly addressed to a different sid."""
    intended = ((event or {}).get("payload") or {}).get("recipient_session_id", "")
    intended = (intended or "").strip()
    if not intended:
        return True  # broadcast / channel-wide event — keep
    return intended == our_sid


def poll_inbox_once(
    api,
    *,
    project_id: str,
    session_id: str,
    since: str,
    cwd: str = "",
) -> tuple:
    """One poll iteration: fetch /bus/unread, persist + ack each event
    addressed to us, return ``(latest_seen, persisted_count)``.

    ``since`` is the in-memory watermark (= bus.mjs's ``bridgeLastSeen``)
    so multiple polls don't re-process the same event. The server-side
    cursor is owned by the inbox hook, not the receive loop, mirroring
    the Claude Code split (see ms-60 follow-up).
    """
    url = (
        f"/api/projects/{project_id}/bus/unread"
        f"?recipient_id={session_id}"
        f"&since={since}"
    )
    try:
        events = api.get(url)
    except Exception:
        return (since, 0)
    if not isinstance(events, list):
        return (since, 0)

    latest_seen = since
    persisted = 0
    for evt in events:
        if not isinstance(evt, dict):
            continue
        created_at = str(evt.get("created_at") or "")
        # ms-60 follow-up: in-memory dedupe — never re-process a poll
        # batch we have already seen (some old servers ignore ``since``).
        if since and created_at <= since:
            continue
        # Stamp delivered before the filter chain (= mirrors bus.mjs
        # e-1348). Sender deserves to know we received it even if we
        # drop it locally.
        ack_event(
            api,
            project_id=project_id,
            event_id=str(evt.get("event_id") or ""),
            stage="delivered",
            recipient_session_id=session_id,
        )
        # Self-loop guard.
        if str(evt.get("sender_session_id") or "") == session_id:
            continue
        # Filter: not addressed to us → drop.
        if not _addressed_to_us(evt, session_id):
            continue
        # Persist for the hook to read on the next user prompt.
        path = persist_inbox_event(evt, cwd=cwd)
        if path is not None:
            persisted += 1
        if created_at > latest_seen:
            latest_seen = created_at
    return (latest_seen, persisted)


# ------------------------------------------------------------------ #
# Inbox file lifecycle (= hook ↔ daemon contract)
# ------------------------------------------------------------------ #


def list_inbox_events(cwd: str = "") -> list:
    """Read every JSON file in the inbox directory (= for the hook)."""
    d = inbox_dir(cwd)
    if not d.is_dir():
        return []
    out = []
    for path in sorted(d.glob("*.json")):
        try:
            with path.open("r", encoding="utf-8") as f:
                out.append({"path": str(path), "event": json.load(f)})
        except (OSError, json.JSONDecodeError):
            continue
    return out


def archive_inbox_event(path: str, cwd: str = "") -> Path | None:
    """Move an inbox file into ``<inbox>/.read/`` so the hook never
    re-injects the same event twice. Returns the archive path, or
    ``None`` on failure (file vanished, etc.)."""
    src = Path(path)
    if not src.is_file():
        return None
    dest_dir = inbox_dir(cwd) / ".read"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    try:
        src.rename(dest)
        return dest
    except OSError:
        return None


__all__ = [
    "ack_event",
    "archive_inbox_event",
    "heartbeat_to_server",
    "inbox_dir",
    "list_inbox_events",
    "persist_inbox_event",
    "poll_inbox_once",
]
