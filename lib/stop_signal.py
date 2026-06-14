"""Beacon Stop Signal protocol (ms-55 e-1646).

The "止まる側" half of ms-55 SPEC (= 並走するエージェントが「走れて、止まれる」
自律性). This module defines:

  * The bus event schema for ``stop-signal`` channel events.
  * Two stop kinds — ``scoped`` (= target a specific MS / task / session) and
    ``global`` (= halt all active autonomous sessions in the project).
  * Helpers to build, validate, and filter stop signal payloads.

Design contract (SPEC §2-3):

  1. **Anyone can broadcast.** Stop authority is not gated by envelope tier —
     a session that detects a runaway must be able to call STOP even if it
     is itself an unprivileged worker. This matches the Andon cord
     principle (Toyota), aviation go-around (any pilot), surgical stop the
     procedure (any nurse). Authorization for *resume* / *clear* is a
     separate concern handled at the receiver / human review layer.

  2. **Two kinds keep blast radius proportionate.** ``scoped`` targets a
     specific MS / task / session id; ``global`` halts everything. Forcing
     every stop to be global causes over-broadcast (a small failure in MS
     A halts unrelated MS B); requiring kind selection makes the sender
     state intent.

  3. **Receivers halt after the current tool call completes.** The receive
     side is responsible for: (a) finishing the in-flight tool call so
     state isn't torn, (b) persisting any in-progress work, (c) emitting
     a stop-acknowledgement event so the sender knows the halt landed.
     This module doesn't implement the receive loop — that's a separate
     hook layer (bin/beacon-bus-inbox-hook.py for AI sessions).

  4. **STUCK is a stop kind.** Idle timeout detection (e-1649) emits a
     stop signal with ``reason_kind == "stuck"``; the same protocol is
     reused so morning briefing (e-1650) doesn't have to special-case it.

Wire format (= what lands in the bus event payload):

  {
    "kind": "stop",                 # discriminator — always "stop"
    "scope": "scoped" | "global",   # per SPEC §3
    "target": {                     # required iff scope == "scoped"
      "kind": "ms" | "task" | "session",
      "id":   "ms-55" / "e-1646" / "sv-..."
    },
    "reason": "free-text human readable explanation",
    "reason_kind": "manual" | "build_fail" | "deploy_fail" |
                    "test_fail" | "approved_actions_violation" |
                    "stuck" | "other",
    "issued_by_session_id": "sv-...",
    "issued_at": "2026-06-15T00:00:00Z",
    "machine_reason": { ... }       # optional; structured detail
  }

The CLI translates user-facing flags into this payload and uses the
existing ``beacon bus send`` plumbing to broadcast it on the
``stop-signal`` channel. Receivers (any AI session listening on
``stop-signal``) parse the payload via ``parse_stop_event`` below.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Schema constants
# ---------------------------------------------------------------------------

#: The bus channel name used for stop / STUCK signals. Receivers should
#: subscribe to this channel to participate in the halt protocol.
STOP_CHANNEL = "stop-signal"

#: Payload discriminator. Anything on STOP_CHANNEL that doesn't carry
#: ``kind == "stop"`` is ignored — keeps room for future protocol kinds
#: on the same channel (e.g. ``"resume"``).
PAYLOAD_KIND_STOP = "stop"
PAYLOAD_KIND_RESUME = "resume"

#: Scope discriminator values. See SPEC §3.
SCOPE_SCOPED = "scoped"
SCOPE_GLOBAL = "global"
_VALID_SCOPES = frozenset({SCOPE_SCOPED, SCOPE_GLOBAL})

#: Target kinds — for ``scope == "scoped"`` only. ``session`` allows
#: surgical halt of a single misbehaving worker without affecting siblings;
#: ``ms`` / ``task`` allow halting all workers attached to a workstream.
TARGET_MS = "ms"
TARGET_TASK = "task"
TARGET_SESSION = "session"
_VALID_TARGET_KINDS = frozenset({TARGET_MS, TARGET_TASK, TARGET_SESSION})

#: Reason kinds. Free text goes in ``reason``; this enum lets morning
#: briefing categorize without parsing English.
REASON_KINDS = (
    "manual",                          # user typed `beacon stop`
    "build_fail",                      # CI / local build failed N times
    "deploy_fail",                     # health check failed post-deploy
    "test_fail",                       # critical test failed
    "approved_actions_violation",      # tool call outside envelope
    "stuck",                           # idle timeout (e-1649)
    "other",                           # catch-all
)
_VALID_REASON_KINDS = frozenset(REASON_KINDS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow_iso() -> str:
    """ISO 8601 UTC timestamp, second precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_stop_payload(
    *,
    scope: str,
    issued_by_session_id: str,
    reason: str = "",
    reason_kind: str = "manual",
    target_kind: Optional[str] = None,
    target_id: Optional[str] = None,
    machine_reason: Optional[dict] = None,
    issued_at: Optional[str] = None,
) -> dict:
    """Build a validated stop signal payload.

    Validation is strict on the producer side so receivers can rely on
    the schema. Bad payloads (= missing target for scoped, unknown
    scope, etc.) raise ``ValueError`` immediately.

    Returns a fresh dict — callers can stamp additional advisory fields
    onto it before sending without polluting cached state.
    """
    if scope not in _VALID_SCOPES:
        raise ValueError(
            f"scope must be one of {sorted(_VALID_SCOPES)}, got {scope!r}"
        )
    if not issued_by_session_id:
        raise ValueError("issued_by_session_id is required")
    if reason_kind not in _VALID_REASON_KINDS:
        raise ValueError(
            f"reason_kind must be one of {sorted(_VALID_REASON_KINDS)}, "
            f"got {reason_kind!r}"
        )

    payload: dict[str, Any] = {
        "kind": PAYLOAD_KIND_STOP,
        "scope": scope,
        "reason": reason or "",
        "reason_kind": reason_kind,
        "issued_by_session_id": issued_by_session_id,
        "issued_at": issued_at or _utcnow_iso(),
    }

    if scope == SCOPE_SCOPED:
        if not target_kind or not target_id:
            raise ValueError(
                "scope=scoped requires target_kind and target_id "
                "(use scope=global for project-wide halt)"
            )
        if target_kind not in _VALID_TARGET_KINDS:
            raise ValueError(
                f"target_kind must be one of {sorted(_VALID_TARGET_KINDS)}, "
                f"got {target_kind!r}"
            )
        payload["target"] = {"kind": target_kind, "id": target_id}
    else:
        # SCOPE_GLOBAL must NOT carry a target — keep the wire format
        # unambiguous so receivers can switch on the absence of `target`.
        if target_kind or target_id:
            raise ValueError(
                "scope=global must not carry target_kind/target_id"
            )

    if machine_reason is not None:
        if not isinstance(machine_reason, dict):
            raise ValueError("machine_reason must be a dict if provided")
        payload["machine_reason"] = machine_reason

    return payload


def build_resume_payload(
    *,
    scope: str,
    issued_by_session_id: str,
    reason: str = "",
    target_kind: Optional[str] = None,
    target_id: Optional[str] = None,
    issued_at: Optional[str] = None,
) -> dict:
    """Build a validated resume payload (= "all clear" signal).

    Resume is the structural inverse of stop: same channel, same target
    shape, but ``kind = "resume"``. Receivers can flip out of halted
    state when they see one matching their scope.

    Resume authorization is *intentionally* unrestricted at this layer
    too. Higher-level workflow (human approval / leader-only) can layer
    on top via a hook that filters resume events from non-leaders, but
    the wire protocol itself does not impose a check — it would be a
    foot-gun (an emergency resume during partial recovery becomes
    impossible if the only authorized resumer is unreachable).
    """
    if scope not in _VALID_SCOPES:
        raise ValueError(
            f"scope must be one of {sorted(_VALID_SCOPES)}, got {scope!r}"
        )
    if not issued_by_session_id:
        raise ValueError("issued_by_session_id is required")

    payload: dict[str, Any] = {
        "kind": PAYLOAD_KIND_RESUME,
        "scope": scope,
        "reason": reason or "",
        "issued_by_session_id": issued_by_session_id,
        "issued_at": issued_at or _utcnow_iso(),
    }
    if scope == SCOPE_SCOPED:
        if not target_kind or not target_id:
            raise ValueError(
                "scope=scoped requires target_kind and target_id"
            )
        if target_kind not in _VALID_TARGET_KINDS:
            raise ValueError(
                f"target_kind must be one of {sorted(_VALID_TARGET_KINDS)}, "
                f"got {target_kind!r}"
            )
        payload["target"] = {"kind": target_kind, "id": target_id}
    else:
        if target_kind or target_id:
            raise ValueError(
                "scope=global must not carry target_kind/target_id"
            )
    return payload


def parse_stop_event(event: dict) -> Optional[dict]:
    """Inverse of build_stop_payload — pull a normalized stop record out
    of a raw bus event dict.

    Returns ``None`` if the event isn't a stop signal (wrong channel,
    wrong kind, missing required fields, etc.). Returning ``None``
    instead of raising lets the receive loop skip malformed events
    without aborting the whole inbox drain — a hostile / buggy sender
    must not be able to break unrelated DM processing.
    """
    if not isinstance(event, dict):
        return None
    if event.get("channel") != STOP_CHANNEL:
        return None
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        return None
    if payload.get("kind") != PAYLOAD_KIND_STOP:
        return None
    scope = payload.get("scope")
    if scope not in _VALID_SCOPES:
        return None
    issued_by = payload.get("issued_by_session_id") or ""
    if not issued_by:
        return None

    record: dict[str, Any] = {
        "scope": scope,
        "reason": payload.get("reason") or "",
        "reason_kind": payload.get("reason_kind") or "other",
        "issued_by_session_id": issued_by,
        "issued_at": payload.get("issued_at") or "",
        "event_id": event.get("event_id") or "",
        "raw_event": event,
    }

    if scope == SCOPE_SCOPED:
        target = payload.get("target") or {}
        if not isinstance(target, dict):
            return None
        tk = target.get("kind")
        ti = target.get("id")
        if tk not in _VALID_TARGET_KINDS or not ti:
            return None
        record["target"] = {"kind": tk, "id": ti}

    if isinstance(payload.get("machine_reason"), dict):
        record["machine_reason"] = payload["machine_reason"]

    return record


def parse_resume_event(event: dict) -> Optional[dict]:
    """Counterpart to parse_stop_event for resume events."""
    if not isinstance(event, dict):
        return None
    if event.get("channel") != STOP_CHANNEL:
        return None
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        return None
    if payload.get("kind") != PAYLOAD_KIND_RESUME:
        return None
    scope = payload.get("scope")
    if scope not in _VALID_SCOPES:
        return None
    issued_by = payload.get("issued_by_session_id") or ""
    if not issued_by:
        return None
    record: dict[str, Any] = {
        "scope": scope,
        "reason": payload.get("reason") or "",
        "issued_by_session_id": issued_by,
        "issued_at": payload.get("issued_at") or "",
        "event_id": event.get("event_id") or "",
        "raw_event": event,
    }
    if scope == SCOPE_SCOPED:
        target = payload.get("target") or {}
        if not isinstance(target, dict):
            return None
        tk = target.get("kind")
        ti = target.get("id")
        if tk not in _VALID_TARGET_KINDS or not ti:
            return None
        record["target"] = {"kind": tk, "id": ti}
    return record


# ---------------------------------------------------------------------------
# Receiver-side helpers (= "am I supposed to halt?")
# ---------------------------------------------------------------------------

def stop_applies_to_session(
    stop_record: dict,
    *,
    my_session_id: str,
    my_ms_id: Optional[str] = None,
    my_task_id: Optional[str] = None,
) -> bool:
    """Decide whether a stop record applies to the current session.

    Used by AI session loops to gate the halt:

      stop = parse_stop_event(event)
      if stop and stop_applies_to_session(stop, my_session_id=..., my_ms_id=...):
          halt_after_current_tool_call(stop)

    Global stops always apply. Scoped stops apply when the target matches
    the session's identity (session id, the MS it's currently working on,
    or a specific task it's executing).

    Returns False for malformed or non-matching records. Receivers MUST
    NOT halt on a False — that would re-introduce the over-broadcast
    problem SPEC §3 explicitly avoids.
    """
    if not isinstance(stop_record, dict):
        return False
    scope = stop_record.get("scope")
    if scope == SCOPE_GLOBAL:
        return True
    if scope != SCOPE_SCOPED:
        return False
    target = stop_record.get("target") or {}
    tk = target.get("kind")
    ti = target.get("id")
    if not tk or not ti:
        return False
    if tk == TARGET_SESSION:
        return bool(my_session_id) and my_session_id == ti
    if tk == TARGET_MS:
        return bool(my_ms_id) and my_ms_id == ti
    if tk == TARGET_TASK:
        return bool(my_task_id) and my_task_id == ti
    return False


# ---------------------------------------------------------------------------
# Stop signal index — find the most recent stop / resume per scope target
# ---------------------------------------------------------------------------

def latest_active_stops(events: list[dict]) -> list[dict]:
    """Reduce a chronological event list to the set of *currently active*
    stop signals (= stops not yet superseded by a matching resume).

    Algorithm:
      * Walk events oldest → newest.
      * For each scope/target key, keep the most recent stop or resume.
      * At the end, return entries whose latest event was a stop.

    This is the building block for ``beacon stop status`` and for any
    receiver that joins the bus mid-stream (= must reconstruct the halt
    state from history rather than relying on having seen every event
    live).

    Args:
      events: bus events as returned by list_unread_bus_events / similar.
        Each event must have ``channel``, ``payload``, ``created_at``.
        Events on other channels are skipped.

    Returns:
      A list of stop records (one per active scope/target key), sorted
      ascending by ``issued_at``.
    """
    state: dict[tuple, dict] = {}

    # Sort events by created_at so the walk is deterministic even if the
    # caller passed an unsorted list. Missing created_at sorts to the
    # front (treated as "very old").
    sorted_events = sorted(
        (e for e in events if isinstance(e, dict)),
        key=lambda e: e.get("created_at") or "",
    )

    for ev in sorted_events:
        if ev.get("channel") != STOP_CHANNEL:
            continue
        stop = parse_stop_event(ev)
        resume = None if stop else parse_resume_event(ev)
        record = stop or resume
        if not record:
            continue

        scope = record.get("scope")
        if scope == SCOPE_GLOBAL:
            key: tuple = ("global", "")
        else:
            target = record.get("target") or {}
            key = (target.get("kind") or "", target.get("id") or "")

        if stop:
            state[key] = stop
        else:
            # resume supersedes any active stop on this key
            state.pop(key, None)

    return sorted(state.values(), key=lambda r: r.get("issued_at") or "")
