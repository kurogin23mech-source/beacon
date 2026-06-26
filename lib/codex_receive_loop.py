"""Codex receive-loop primitives (= ms-93 / e-2497, layer 2).

The receive-loop work split (= ms-93 / e-2502): bus protocol responsibilities
(= filter chain, heartbeat body, ack shape, URL builders) live in
``lib/bus_protocol.py`` so Claude Code's bus.mjs and Codex share the same
contract. This module wraps those primitives with the Codex-specific
adapter pieces (= file-based inbox persistence + archive on hook read).

The daemon (= scripts/codex-receive-loop.py) wires these into a
``while True`` plus signal handlers; the loop body lives here so tests
can drive one iteration deterministically.

2026-06-26 Codex dogfood (= DM ``D7sAqeIqn6gIMFRIs9PD``) fixes folded
into this rewrite:

- broadcast pollution: events that don't match the protocol's filter
  chain (= DM-without-recipient drop / channel allowlist / recipient
  mismatch) are no longer persisted to the Codex inbox
- initial watermark: cold start now pins ``since`` to "now" so the
  first poll does not flood the inbox with weeks of history
- archive idempotency: archiving a file when a conflicting name
  already exists in ``.read/`` no longer raises; the duplicate is
  retired with a unique suffix
- ``opened`` ack: archive (= equivalent of the AI "seeing" the event)
  now fires the opened stage
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

# Shared protocol primitives (= e-2502 phase 2 core extraction).
import bus_protocol as bp


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
    """PUT /api/projects/<pid>/sessions/<sid> with the canonical body.

    Body shape comes from ``bus_protocol.heartbeat_body`` so Codex and
    Claude Code stay aligned. ``actor`` is sent only on the first
    heartbeat (= mint path) or when the caller wants to refresh it;
    the server's field-path merge preserves actor.machine/agent.

    Returns ``True`` on success, ``False`` on transport failure
    (= we never raise; the loop must survive transient network blips).
    """
    body = bp.heartbeat_body(
        _now_iso(),
        poll_interval_ms=poll_interval_ms,
        shutdown=shutdown,
    )
    if actor:
        body["actor"] = actor
    try:
        api.put(bp.heartbeat_path(project_id, session_id), body)
        return True
    except Exception:
        return False


# ------------------------------------------------------------------ #
# Inbox persistence (= Codex adapter)
# ------------------------------------------------------------------ #


def inbox_dir(cwd: str = "") -> Path:
    """Resolve ``<cwd>/.beacon/codex/inbox``, defaulting to ``Path.cwd()``."""
    return (Path(cwd) if cwd else Path.cwd()) / INBOX_DIR_REL


def persist_inbox_event(event: dict, cwd: str = "") -> Path | None:
    """Write ``event`` to the inbox directory as a per-event JSON file."""
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
    """POST /api/projects/<pid>/bus/<event_id>/ack. Fire-and-forget.

    On transport failure, prints a single line to stderr so the
    surrounding context (= daemon stdout/stderr stream, or the hook
    runner's stderr buffer) preserves the audit trail. Codex 2026-06-26
    dogfood (= DM Gx0VhYhthfqneAdp4XVS) found the silent ``except``
    here caused a false negative: a network-sandboxed hook invocation
    archived the event and emitted additionalContext but the user had
    no signal that opened ack failed. The print converts the swallowed
    failure into an observable one without changing the fire-and-forget
    contract — callers can still ignore the return value.
    """
    if not event_id:
        return False
    try:
        body = bp.ack_body(stage, recipient_session_id)
    except ValueError:
        return False
    try:
        api.post(bp.ack_path(project_id, event_id), body)
        return True
    except Exception as exc:
        import sys as _sys
        _sys.stderr.write(
            f"beacon-bus: ack {stage} for {event_id} failed "
            f"({type(exc).__name__}): {exc}\n"
        )
        _sys.stderr.flush()
        return False


# ------------------------------------------------------------------ #
# poll_inbox_once — core filter + adapter persist
# ------------------------------------------------------------------ #


def poll_inbox_once(
    api,
    *,
    project_id: str,
    session_id: str,
    since: str,
    cwd: str = "",
    allowed_channels: tuple = bp.DEFAULT_ALLOWED_CHANNELS,
    on_kept_event=None,
) -> tuple:
    """One poll iteration: fetch + run the protocol filter chain +
    persist surviving events + ack delivered.

    Returns ``(latest_seen, persisted_count)`` so the caller can roll
    the in-memory watermark forward.

    The filter chain lives in ``bus_protocol.filter_event``; this
    function only handles the Codex-side persistence after the protocol
    decides "keep". That split is what e-2502 SPEC §2 calls "core +
    adapter": filtering belongs to bus protocol, persistence belongs to
    the Codex adapter.

    ``on_kept_event`` (= ms-93 / e-2519 app-server wiring) is an
    optional callable invoked once per event that passes the filter
    chain AND was successfully persisted. Daemons running in
    autonomous mode (= ``--app-server``) use this to also dispatch the
    DM to a long-lived ``codex app-server --stdio`` child; the default
    pull-on-prompt path leaves the callback as ``None`` and behaves
    exactly as before. Callback exceptions are caught so a flaky
    autonomous path cannot stall the pull path.
    """
    url = bp.poll_unread_path(project_id, session_id, since)
    try:
        events = api.get(url)
    except Exception:
        return (since, 0)
    if not isinstance(events, list):
        return (since, 0)

    config = bp.FilterConfig(
        our_session_id=session_id,
        allowed_channels=allowed_channels,
        watermark=since,
    )

    latest_seen = since
    persisted = 0
    for evt in events:
        if not isinstance(evt, dict):
            continue
        created_at = str(evt.get("created_at") or "")
        verdict = bp.filter_event(evt, config)
        if verdict == bp.FILTER_DROP_WATERMARK:
            # Already-seen events: skip without an ack (server should
            # have honoured ``since`` server-side already).
            continue
        # Stamp delivered before the rest of the decision tree (e-1348
        # rule): the sender deserves to know we received the event even
        # when we drop it locally.
        ack_event(
            api,
            project_id=project_id,
            event_id=str(evt.get("event_id") or ""),
            stage=bp.ACK_STAGE_DELIVERED,
            recipient_session_id=session_id,
        )
        if verdict != bp.FILTER_KEEP:
            continue
        # Persist for the hook to read on the next user prompt.
        path = persist_inbox_event(evt, cwd=cwd)
        if path is not None:
            persisted += 1
            if on_kept_event is not None:
                try:
                    on_kept_event(evt)
                except Exception:
                    # Autonomous-path failures must not stall pull-path
                    # persistence. The caller's logger surfaces details.
                    pass
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


def archive_inbox_event(
    path: str,
    cwd: str = "",
    *,
    api=None,
    project_id: str = "",
    recipient_session_id: str = "",
) -> Path | None:
    """Move an inbox file into ``<inbox>/.read/`` (= opened semantic).

    Idempotent name resolution: if the destination already exists
    (= same event_id was re-persisted after a daemon restart, see
    Codex 2026-06-26 blocker #4), append a millisecond suffix so the
    rename always succeeds and no event is silently overwritten.

    When ``api`` + ``project_id`` + ``recipient_session_id`` are
    supplied, the move also fires the ``opened`` ack stage. The hook
    invokes this — that's the moment "the AI has seen this event" in
    the Codex adapter (= equivalent of bus.mjs's mcp.notification
    success).
    """
    src = Path(path)
    if not src.is_file():
        return None
    dest_dir = inbox_dir(cwd) / ".read"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if dest.exists():
        # Codex 2026-06-26 blocker #4: avoid losing the new version to
        # a stale archive from a prior run. Suffix with the current
        # millisecond so each rename is unique.
        suffix = f".{int(time.time() * 1000)}"
        dest = dest.with_suffix(dest.suffix + suffix)
    try:
        src.rename(dest)
    except OSError:
        return None

    # Fire opened ack if the caller wired up the bus client. Best-effort —
    # archive succeeded even if the ack network call fails.
    if api is not None and project_id and recipient_session_id:
        # Recover event_id from the source filename (= ``<event_id>.json``).
        event_id = Path(path).stem
        if event_id:
            ack_event(
                api,
                project_id=project_id,
                event_id=event_id,
                stage=bp.ACK_STAGE_OPENED,
                recipient_session_id=recipient_session_id,
            )
    return dest


__all__ = [
    "ack_event",
    "archive_inbox_event",
    "heartbeat_to_server",
    "inbox_dir",
    "list_inbox_events",
    "persist_inbox_event",
    "poll_inbox_once",
]
