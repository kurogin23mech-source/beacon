"""Beacon bus protocol — shared core (= ms-93 / e-2502 phase 2).

The single source of truth for "how a Beacon bus client talks to the
server and filters incoming events". Both Claude Code's
``channel/bus.mjs`` (Node) and Codex's ``lib/codex_receive_loop.py``
(Python) must end up obeying this contract so behaviour does not drift
between AI clients (= e-2502 SPEC ``GocXs8qXRrEmOfBt7lK6``).

What lives here (= protocol):
- heartbeat body shape + endpoint
- /bus/unread URL builder + initial watermark policy
- filter chain that runs on every fetched event (= must lockstep with
  server-side ``server/app.py::_bus_event_addressed_to``)
- ack endpoint + body shape

What does NOT live here (= AI client adapter responsibility):
- how a fetched event is presented to the AI (= bus.mjs uses
  mcp.notification; codex_receive_loop persists to a file the hook reads)
- session identity resolution (= pid-tree bridge claim for Claude Code,
  cwd hash + parent_pid for Codex; identity *storage schema* IS protocol,
  resolution logic is adapter)
- content shape sent to the AI (= slim ping / full body / autonomous
  imperative for Claude Code; raw event JSON for Codex)

Codex 2026-06-26 dogfood (= D7sAqeIqn6gIMFRIs9PD) found three silent
drifts already baked into the parallel Python implementation:

1. DM-without-recipient drop (= bus.mjs filter 2b, e-1209) was missing
2. channel allowlist filter (= bus.mjs filter 3) was missing
3. ``opened`` ack stage was not fired (= only ``delivered`` was wired)

All three are now in the canonical ``filter_event`` / ``ack_stage_*``
contract here. Any future port (= Gemini CLI etc.) reuses this.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


# Channels the bridge listens on by default (= mirrors the bus.mjs
# ALLOWED_CHANNELS list and the inbox-hook expectations). Callers can
# override with ``allowed_channels=...`` when constructing the filter
# config, but the default keeps a single port from accidentally drifting
# from the bus.mjs side.
DEFAULT_ALLOWED_CHANNELS = (
    "dm",
    "operation-trigger",
    "trek-progress-check",
    "trek-trigger",
    "trek-task-review",
)


# ------------------------------------------------------------------ #
# Heartbeat (= channel/bus-heartbeat.mjs::buildHeartbeatBody mirror)
# ------------------------------------------------------------------ #


def heartbeat_body(now_iso: str, *, poll_interval_ms: int, shutdown: bool = False) -> dict:
    """Return the canonical PUT /sessions/<sid> body.

    Shape MUST match ``channel/bus-heartbeat.mjs::buildHeartbeatBody``
    so the server's poll-health computation (= e-1318) treats every
    bus client identically.
    """
    return {
        "last_active": now_iso,
        "last_poll_at": now_iso,
        "poll_interval_ms": int(poll_interval_ms),
        "shutdown": bool(shutdown),
    }


def heartbeat_path(project_id: str, session_id: str) -> str:
    """Canonical endpoint path for heartbeat PUT."""
    return f"/api/projects/{project_id}/sessions/{session_id}"


# ------------------------------------------------------------------ #
# Inbox poll URL builder + initial watermark
# ------------------------------------------------------------------ #


def initial_watermark_now() -> str:
    """Initial ``since`` value when the bridge starts cold (= ignore history).

    Codex 2026-06-26 blocker #2: starting with ``since=""`` makes the
    server return every unread event (= weeks of broadcast traffic) on
    the first poll. The bridge must instead pin its in-memory watermark
    to "now" at cold start so the first poll only catches *new* events.
    This matches bus.mjs's effective behaviour: bridgeLastSeen is empty
    initially, BUT bus.mjs ONLY fetches events from the per-recipient
    DM queue (server-side filtered), so flooding doesn't happen there.
    Codex's receive loop polls the same endpoint but persists results
    to disk for a hook to read later — broadcast traffic IS unwanted.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def poll_unread_path(project_id: str, recipient_session_id: str, since: str) -> str:
    """Canonical URL for fetching unread events."""
    return (
        f"/api/projects/{project_id}/bus/unread"
        f"?recipient_id={recipient_session_id}"
        f"&since={since or ''}"
    )


# ------------------------------------------------------------------ #
# Filter chain (= bus.mjs filter 1 / 2a / 2b / 3 mirror)
# ------------------------------------------------------------------ #


@dataclass(frozen=True)
class FilterConfig:
    """Tunable parameters for the per-event filter."""

    our_session_id: str
    allowed_channels: tuple = DEFAULT_ALLOWED_CHANNELS
    watermark: str = ""  # in-memory ``since`` — dedupe on top of server


# Filter decision codes — callers can match on these for logging /
# observability without re-doing the logic.
FILTER_KEEP = "keep"
FILTER_DROP_WATERMARK = "drop:watermark"
FILTER_DROP_SELF = "drop:self-sent"
FILTER_DROP_RECIPIENT_MISMATCH = "drop:recipient-mismatch"
FILTER_DROP_DM_UNADDRESSED = "drop:dm-unaddressed"  # e-1209
FILTER_DROP_CHANNEL = "drop:channel-not-allowed"


def filter_event(event: dict, config: FilterConfig) -> str:
    """Return one of the ``FILTER_*`` codes for this event.

    Lockstep with ``channel/bus.mjs::pollOnce`` filters 1, 2a, 2b, 3.
    Lockstep with server-side ``_bus_event_addressed_to`` so a drop
    here means the same drop server-side would make if it could.
    """
    if not isinstance(event, dict):
        return FILTER_DROP_RECIPIENT_MISMATCH  # malformed → safest drop

    created_at = str(event.get("created_at") or "")
    # ms-60 follow-up: in-memory watermark dedupe — survives the
    # ``since`` query param being silently ignored by older servers.
    if config.watermark and created_at and created_at <= config.watermark:
        return FILTER_DROP_WATERMARK

    sender = str(event.get("sender_session_id") or "")
    if sender == config.our_session_id:
        return FILTER_DROP_SELF

    channel = str(event.get("channel") or "")
    payload = event.get("payload") or {}
    intended = str(
        (payload.get("recipient_session_id") if isinstance(payload, dict) else "") or ""
    ).strip()

    is_dm = channel == "dm"

    # Filter 2a: addressed to someone else (DM or otherwise).
    if intended and intended != config.our_session_id:
        return FILTER_DROP_RECIPIENT_MISMATCH

    # Filter 2b (= e-1209): DM with no recipient stamp is treated as
    # malformed-drop. Codex receive loop missed this and ended up
    # persisting broadcast traffic (operation-trigger / trek-* / test-*)
    # into a Codex DM inbox.
    if is_dm and not intended:
        return FILTER_DROP_DM_UNADDRESSED

    # Filter 3: channel allowlist. Anything outside the configured
    # channel list (= e.g. test broadcasts) is dropped.
    if channel and channel not in config.allowed_channels:
        return FILTER_DROP_CHANNEL

    return FILTER_KEEP


# ------------------------------------------------------------------ #
# Ack endpoint + body shape
# ------------------------------------------------------------------ #


ACK_STAGE_DELIVERED = "delivered"  # bridge fetched the event
ACK_STAGE_OPENED = "opened"  # adapter delivered it to the AI


def ack_path(project_id: str, event_id: str) -> str:
    return f"/api/projects/{project_id}/bus/{event_id}/ack"


def ack_body(stage: str, recipient_session_id: str) -> dict:
    if stage not in (ACK_STAGE_DELIVERED, ACK_STAGE_OPENED):
        raise ValueError(f"unknown ack stage: {stage!r}")
    return {"stage": stage, "recipient_session_id": recipient_session_id}


__all__ = [
    "ACK_STAGE_DELIVERED",
    "ACK_STAGE_OPENED",
    "DEFAULT_ALLOWED_CHANNELS",
    "FILTER_DROP_CHANNEL",
    "FILTER_DROP_DM_UNADDRESSED",
    "FILTER_DROP_RECIPIENT_MISMATCH",
    "FILTER_DROP_SELF",
    "FILTER_DROP_WATERMARK",
    "FILTER_KEEP",
    "FilterConfig",
    "ack_body",
    "ack_path",
    "filter_event",
    "heartbeat_body",
    "heartbeat_path",
    "initial_watermark_now",
    "poll_unread_path",
]
