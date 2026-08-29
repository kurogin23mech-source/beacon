"""Beacon Stop Signal protocol (ms-55 e-1646 / e-1721 receive side).

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


# ---------------------------------------------------------------------------
# Halt-request file path — receiver-side persistence (ms-55 e-1721)
# ---------------------------------------------------------------------------
#
# When the inbox hook sees a stop event that applies to this session, it
# writes a `halt-request.json` under `.beacon/sessions/<sid>/`. The
# PostToolUse hook reads that file after each tool call and surfaces it
# to the AI as additionalContext, completing the protocol:
#
#   sender -> bus event -> receiver inbox hook -> halt-request.json
#                                              -> PostToolUse hook
#                                              -> AI sees "STOP signal"
#
# The file format intentionally mirrors `parse_stop_event`'s output
# (= scope, reason, reason_kind, target, issued_by_session_id, ...)
# so the PostToolUse hook can render it without re-parsing the bus
# event. A separate `acknowledged_at` field is stamped when the AI
# acknowledges the halt; subsequent PostToolUse fires can suppress the
# inject so the AI isn't reminded repeatedly.

HALT_REQUEST_FILENAME = "halt-request.json"


def halt_request_path(
    session_id: str,
    *,
    beacon_dir: Optional[str] = None,
) -> str:
    """Return the absolute path to the halt-request file for `session_id`.

    Default base = `.beacon` from the cwd. Override via the `beacon_dir`
    kwarg (mostly for tests).
    """
    import os  # noqa: PLC0415

    base = beacon_dir or os.environ.get("BEACON_DIR", "") or ".beacon"
    return os.path.join(base, "sessions", session_id, HALT_REQUEST_FILENAME)


def write_halt_request(
    stop_record: dict,
    *,
    session_id: str,
    beacon_dir: Optional[str] = None,
) -> str:
    """Persist a halt request for the receiver session.

    Idempotent — re-writing the same stop record overwrites in place.
    Caller is expected to have already filtered via
    `stop_applies_to_session` so the file represents an actionable halt,
    not noise.

    Returns the path written.
    """
    import json as _json  # noqa: PLC0415
    import os  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    if not session_id:
        raise ValueError("session_id is required")
    if not isinstance(stop_record, dict):
        raise TypeError("stop_record must be a dict")

    path = halt_request_path(session_id, beacon_dir=beacon_dir)
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)

    # Drop the raw_event subtree before writing — it carries the full
    # bus envelope which inflates the file size + duplicates data already
    # in the bus log. Keep only fields the PostToolUse hook renders.
    body = {
        k: v for k, v in stop_record.items()
        if k != "raw_event"
    }
    body.setdefault("received_at", _utcnow_iso())

    fd, tmp = tempfile.mkstemp(prefix=".halt-request.", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            _json.dump(body, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def read_halt_request(
    session_id: str,
    *,
    beacon_dir: Optional[str] = None,
) -> Optional[dict]:
    """Return the active halt request for `session_id`, or None.

    A request that has already been acknowledged (= `acknowledged_at`
    field set) is still returned; callers decide whether to suppress
    based on that field. We don't auto-delete because the file doubles
    as an audit artifact for "this session was asked to halt at <ts>".
    """
    import json as _json  # noqa: PLC0415
    import os  # noqa: PLC0415

    if not session_id:
        return None
    path = halt_request_path(session_id, beacon_dir=beacon_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = _json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def acknowledge_halt_request(
    session_id: str,
    *,
    beacon_dir: Optional[str] = None,
    note: str = "",
) -> bool:
    """Stamp `acknowledged_at` on the halt request so the PostToolUse
    hook can stop re-surfacing it.

    Returns True if a request existed (acknowledged or not before).
    """
    import json as _json  # noqa: PLC0415
    import os  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    data = read_halt_request(session_id, beacon_dir=beacon_dir)
    if data is None:
        return False
    data["acknowledged_at"] = _utcnow_iso()
    if note:
        data["acknowledgement_note"] = note

    path = halt_request_path(session_id, beacon_dir=beacon_dir)
    parent = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".halt-request.", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


def clear_halt_request(
    session_id: str,
    *,
    beacon_dir: Optional[str] = None,
) -> bool:
    """Remove the halt request entirely (= resume / session restart).

    Returns True if a file existed.
    """
    import os  # noqa: PLC0415

    if not session_id:
        return False
    path = halt_request_path(session_id, beacon_dir=beacon_dir)
    if not os.path.exists(path):
        return False
    try:
        os.unlink(path)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Receive-side reducer — drive write_halt_request from a bus event batch
# ---------------------------------------------------------------------------

def process_inbox_events(
    events: list[dict],
    *,
    session_id: str,
    ms_id: Optional[str] = None,
    task_id: Optional[str] = None,
    beacon_dir: Optional[str] = None,
) -> Optional[dict]:
    """Scan an inbox event batch for stop / resume signals that apply
    to the current session.

    Writes / clears `.beacon/sessions/<sid>/halt-request.json` as a
    side effect:
      * latest matching stop → write the request file
      * latest matching resume after a stop → clear the file
      * neither → no change

    Returns the persisted halt request dict (= same shape
    `read_halt_request` returns) when one is now active, or None.
    """
    if not session_id:
        return None

    # Walk oldest-first so the latest event wins.
    sorted_events = sorted(
        (e for e in events or [] if isinstance(e, dict)),
        key=lambda e: e.get("created_at") or "",
    )

    pending_stop: Optional[dict] = None
    cleared = False
    for ev in sorted_events:
        if ev.get("channel") != STOP_CHANNEL:
            continue
        stop = parse_stop_event(ev)
        if stop and stop_applies_to_session(
            stop, my_session_id=session_id,
            my_ms_id=ms_id, my_task_id=task_id,
        ):
            pending_stop = stop
            cleared = False
            continue
        resume = parse_resume_event(ev)
        if resume:
            # A scoped resume that matches our active halt clears it.
            scope = resume.get("scope")
            if scope == SCOPE_GLOBAL:
                pending_stop = None
                cleared = True
                continue
            target = resume.get("target") or {}
            # Mirror stop_applies_to_session's matching rule on the
            # target side so a resume for ms-55 clears a halt on ms-55.
            if scope == SCOPE_SCOPED:
                pretend_stop = {
                    "scope": SCOPE_SCOPED,
                    "target": target,
                }
                if stop_applies_to_session(
                    pretend_stop, my_session_id=session_id,
                    my_ms_id=ms_id, my_task_id=task_id,
                ):
                    pending_stop = None
                    cleared = True

    if pending_stop is not None:
        write_halt_request(
            pending_stop, session_id=session_id, beacon_dir=beacon_dir,
        )
        return read_halt_request(session_id, beacon_dir=beacon_dir)
    if cleared:
        clear_halt_request(session_id, beacon_dir=beacon_dir)
    return None


# ---------------------------------------------------------------------------
# PostToolUse hook surface (ms-55 e-1721)
# ---------------------------------------------------------------------------
#
# The Claude Code PostToolUse hook calls render_halt_inject after each
# tool call to decide whether to surface the halt to the AI. The render
# emits a short markdown block with the stop record's reason / target /
# sender — enough for the AI to decide to halt voluntarily after
# finishing the current tool call (= SPEC §3 "halt after current tool
# call completes").

def render_halt_inject(halt_request: dict) -> str:
    """Format a halt request as PostToolUse additionalContext markdown.

    Returns "" for malformed input so the hook can just check truthiness.
    """
    if not isinstance(halt_request, dict):
        return ""
    scope = halt_request.get("scope") or "?"
    reason = halt_request.get("reason") or "(no reason)"
    reason_kind = halt_request.get("reason_kind") or "other"
    sender = halt_request.get("issued_by_session_id") or "?"
    issued_at = halt_request.get("issued_at") or "?"
    received_at = halt_request.get("received_at") or "?"

    # e-5803 review (AX-3): emit the FULL, runnable resume command, not a vague
    # `beacon resume ...`. The bare form errors (the CLI requires the `scoped`
    # or `global` subcommand), so an AI that copies it literally cannot recover.
    if scope == SCOPE_GLOBAL:
        target_line = "scope: GLOBAL (every active session is asked to halt)"
        resume_cmd = "beacon resume global"
    else:
        target = halt_request.get("target") or {}
        tk = target.get("kind", "?")
        ti = target.get("id", "?")
        target_line = f"scope: scoped → {tk}:{ti}"
        resume_cmd = f"beacon resume scoped --target {tk}:{ti}"

    lines = [
        "⚠ STOP SIGNAL — halt requested",
        "",
        target_line,
        f"reason_kind: {reason_kind}",
        f"reason: {reason}",
        f"from: {sender}",
        f"issued_at: {issued_at}",
        f"received_at: {received_at}",
        "",
        "Action: finish the current tool call cleanly, persist any in-progress",
        "work, then halt. Do not start new tool calls until the user clears",
        f"the halt with `{resume_cmd}` (or you explicitly override after",
        "explaining why).",
    ]
    return "\n".join(lines)


def halt_inject_needed(halt_request: Optional[dict]) -> bool:
    """Return True iff the halt should be surfaced to the AI right now.

    Suppression rules:
      * No request file → no inject.
      * `acknowledged_at` already stamped → no inject (the AI already
        saw it; re-surfacing every PostToolUse would be noise).
    """
    if not halt_request:
        return False
    return not halt_request.get("acknowledged_at")
