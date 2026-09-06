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
    cwd: str = "",
) -> bool:
    """PUT /api/projects/<pid>/sessions/<sid> with the canonical body.

    Body shape comes from ``bus_protocol.heartbeat_body`` so Codex and
    Claude Code stay aligned. ``actor`` is sent only on the first
    heartbeat (= mint path) or when the caller wants to refresh it;
    the server's field-path merge preserves actor.machine/agent.

    e-3091 follow-up: also stamp the **top-level** ``agent.kind`` on the
    session doc, mirroring channel/bus.mjs's cold-start stamp
    (``stampColdStartMetadata`` PUTs ``{ agent: {kind, version}, ... }``
    to the same ``/sessions/<sid>`` endpoint). Without this the Codex
    session doc only carries ``actor.agent`` (a machine label) and its
    top-level ``agent.kind`` stays unset — so
    ``identity_resolve.agent_kind_of`` returns "" and
    ``resolve_stable_identity(agent_kind="codex")`` never matches the
    Codex row, silently degrading the DM churn-safe re-resolution that
    e-3091 built. The structural kind is read from ``actor.agent_kind``
    (the daemon always passes "codex").

    e-2516: also stamp the **top-level** ``cwd`` when supplied, mirroring
    channel/bus.mjs's cold-start ``collectLayer1Where`` (which PUTs a
    top-level ``cwd`` to the same ``/sessions/<sid>`` endpoint). Without
    this, ``beacon sessions --live`` shows Codex rows with no working
    directory, so a user running Codex in several cwds cannot tell which
    session is which (= the multi-cwd identification gap this closes).
    Gated on ``actor`` so it rides the cold-start / refresh path, not
    every 2s poll — cwd never changes mid-session.

    Returns ``True`` on success, ``False`` on transport failure
    (= we never raise; the loop must survive transient network blips).
    """
    body = bp.heartbeat_body(
        _now_iso(),
        poll_interval_ms=poll_interval_ms,
        shutdown=shutdown,
        # ms-145 / e-5378 — the Codex receive loop has no WS accelerator (unlike
        # the Node bridge channel/bus.mjs), so it polls at a fixed cadence and
        # never drops to a backstop. Self-report as poll-only so the directory
        # shows *why* this session is on frequent polling (= it is by design, not
        # a broken WS handshake). This is one root of "only some clients poll".
        transport={"ws_state": bp.WS_STATE_POLL_ONLY, "effective_poll_ms": int(poll_interval_ms)},
    )
    if actor:
        body["actor"] = actor
        kind = str(actor.get("agent_kind") or "").strip()
        if kind:
            body["agent"] = {"kind": kind}
        cwd = str(cwd or "").strip()
        if cwd:
            body["cwd"] = cwd
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
# persist-fallback decision (= ms-93 / e-3156 blackhole fix)
# ------------------------------------------------------------------ #


def should_persist_kept(*, has_app_server: bool, armed: bool) -> bool:
    """Decide whether a kept DM must also land in the hook inbox fallback.

    ``opened`` and the visible-session surfacing are two different things. In
    ``--app-server`` mode a kept DM is dispatched to a background turn (which
    acks ``opened``), but that turn does NOT wake the human's foreground TUI.
    So the inbox fallback (= pull-on-prompt via the UserPromptSubmit hook) is
    the only path that surfaces the DM to the human at their next prompt.

    The blackhole (= ms-93 / e-3156): the daemon used to persist the inbox
    fallback only when there was no app-server at all
    (``persist_kept = app_server_client is None``). With an app-server but
    WITHOUT ``--armed``, a DM was dispatched to a silent background turn, got
    no autonomous reply (not armed), and was never kept in the inbox — so it
    vanished from every human-visible path.

    Rule: keep the inbox fallback UNLESS the app-server is BOTH present AND
    armed. Only in armed+app-server mode is the autonomous reply itself the
    surfacing path, so the inbox fallback is redundant there.
    """
    return (not has_app_server) or (not armed)


# ------------------------------------------------------------------ #
# Channel allowlist merge (= channel/bus.mjs ALLOWED_CHANNELS mirror)
# ------------------------------------------------------------------ #


def merge_allowed_channels(cwd: str = "", *,
                           base: tuple = bp.DEFAULT_ALLOWED_CHANNELS,
                           environ: "dict | None" = None) -> tuple:
    """Merge the channel allowlist from defaults + env + project.json.

    Mirrors ``channel/bus.mjs``'s ``ALLOWED_CHANNELS`` construction (= env
    ``BEACON_CHANNEL_ALLOWLIST`` ∪ ``.beacon/project.json``
    ``bus_auto_execute_channels``). Before this (= ms-160 e-5804) the Codex
    receive loop used a FIXED tuple and ignored both sources, so a project that
    opted an extra channel in (e.g. ``operation-trigger`` via
    ``bus_auto_execute_channels``) was silently dropped on Codex while bus.mjs
    delivered it — the two sides were asymmetric.

    SUPERSET merge: the protocol defaults are ALWAYS kept (a session must not be
    able to opt OUT of them, same posture as the stop-signal exemption); env and
    project only ADD channels. Order-preserving + de-duplicated (defaults first,
    then env, then project). Best-effort: a missing / malformed project.json
    contributes nothing.
    """
    environ = os.environ if environ is None else environ
    merged = list(base)

    def _add(items) -> None:
        for it in items:
            ch = str(it).strip()
            if ch and ch not in merged:
                merged.append(ch)

    env_raw = str(environ.get("BEACON_CHANNEL_ALLOWLIST", "") or "")
    if env_raw:
        _add(env_raw.split(","))

    if cwd:
        pj = Path(cwd) / ".beacon" / "project.json"
        try:
            data = json.loads(pj.read_text(encoding="utf-8"))
            raw = data.get("bus_auto_execute_channels")
            if isinstance(raw, list):
                _add(raw)
        except Exception:
            # Missing / malformed project.json → defaults + env only.
            pass

    return tuple(merged)


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
    persist_kept: bool = True,
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
    chain. By default, the event is first persisted for the
    pull-on-prompt hook. App-server daemons can pass
    ``persist_kept=False`` so the hook cannot race the autonomous
    path and archive/read the same DM before the daemon replies.
    If that callback fails, persist the event as a pull-on-prompt fallback;
    advancing the watermark without either delivery path would lose the DM.
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
        # ms-160 e-5856: a remote STOP is a kill-switch, never a DM. It must
        # ALWAYS land in the file inbox — so the PostToolUse halt hook can turn
        # it into a halt-request even mid autonomous loop — and must NEVER be
        # handed to on_kept_event. Dispatching a STOP to the app-server /
        # exec-worker as a reply-worthy turn would be both wrong (it is not a
        # question) and self-defeating (the very loop we are trying to halt
        # would "answer" it). This holds regardless of persist_kept, so the
        # armed+app-server path (persist_kept=False) stays stoppable too.
        if str(evt.get("channel") or "") == bp.STOP_SIGNAL_CHANNEL:
            path = persist_inbox_event(evt, cwd=cwd)
            if path is not None:
                persisted += 1
            if created_at > latest_seen:
                latest_seen = created_at
            continue
        if persist_kept:
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
        elif on_kept_event is not None:
            try:
                on_kept_event(evt)
            except Exception:
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


def build_dm_reply_args(
    *,
    event: dict,
    agent_text: str,
    beacon_bin: str = "beacon",
) -> list[str] | None:
    """Build ``beacon bus send`` argv for replying with ``agent_text``.

    Returns the argv list ready for ``subprocess.run``, or ``None`` when
    the reply must be skipped because there is nothing meaningful to send
    or no one to address. The caller (= daemon) shells this out only
    when the autonomous push path (= ms-93 / e-2519 AC 2) is armed.

    Skip conditions (= silent no-op):
    - ``agent_text`` is empty / whitespace-only (= Codex returned nothing
      useful, sending an empty reply would be noise)
    - the original event lacks ``event_id`` (= cannot thread the reply)
    - the original event lacks ``sender_session_id`` (= no addressee)
    """
    text = (agent_text or "").strip()
    if not text:
        return None
    event_id = str((event or {}).get("event_id") or "").strip()
    sender_sid = str((event or {}).get("sender_session_id") or "").strip()
    if not event_id or not sender_sid:
        return None
    payload = json.dumps({"text": text}, ensure_ascii=False)
    return [
        beacon_bin,
        "bus",
        "send",
        "--channel",
        "dm",
        "--to",
        sender_sid,
        "--in-reply-to",
        event_id,
        "--payload",
        payload,
        "--json",
    ]


# ------------------------------------------------------------------ #
# Autonomous push-receive dispatch (= ms-93 / e-2519 AC 7)
#
# The idle→DM→autonomous-reply loop lived entirely inside the daemon's
# ``_on_kept_event`` closure (scripts/codex-receive-loop.py), so it could
# only be exercised by running the whole daemon. Extracting it here makes
# it a single testable seam: an integration test can drive
# idle→DM→app-server→reply with fakes and no real Codex / bus, and the
# test then pins the loop against regression (= the AC 7 forcing function).
# ------------------------------------------------------------------ #


def _default_run_reply(argv, *, sender_session_id: str):
    """Shell out the reply, pinning the sender to this daemon's sid.

    ``BEACON_BUS_SENDER`` attributes the reply to the Codex bridge rather
    than whatever sid the ``beacon`` CLI would mint on its own.
    """
    import subprocess

    env = {**os.environ, "BEACON_BUS_SENDER": sender_session_id}
    return subprocess.run(
        argv, env=env, capture_output=True, text=True, timeout=15
    )


def dispatch_kept_event(
    evt,
    *,
    app_server_client,
    agent_text_fn,
    ack_fn,
    armed: bool,
    sender_session_id: str,
    beacon_bin: str = "beacon",
    run_reply=None,
    log=None,
) -> dict:
    """Run one kept-DM dispatch: app-server turn → ack → optional reply.

    This is the single testable seam for e-2519's push-receive loop. The
    daemon closure delegates here; a test drives it with fakes.

    Steps:
      1. dispatch the DM to the app-server child, acking ``opened`` at
         read-time (= the moment the DM enters the turn, via the
         ``on_dispatched`` seam), then wait for the turn to idle
         (``app_server_client.dispatch_dm_and_wait``). ``opened`` is a
         read-receipt, so it fires when the AI reads the DM (= turn entry),
         not when the arbitrarily-long turn finishes (= ms-93 / e-3140).
      2. reconstruct the agent reply text from streamed notifications
         (``agent_text_fn``)
      3. if ``armed``, build the reply argv (``build_dm_reply_args``) and
         shell it out via ``run_reply``

    Dependency injection (= keeps this pure of real Codex / bus / network):
      ``app_server_client`` — exposes ``dispatch_dm_and_wait(evt)``
      ``agent_text_fn`` — ``notifications -> str``
      ``ack_fn`` — ``callable(event_id: str)`` (caller binds api / sid)
      ``run_reply`` — ``callable(argv, sender_session_id=...)`` returning an
        object with ``returncode`` / ``stderr``; defaults to a real send
      ``log`` — ``callable(msg, *, err=False)``; defaults to no-op

    ``dispatch_dm_and_wait`` exceptions propagate (the daemon reconnects the
    transport); reply-send exceptions are caught and logged (best-effort
    reply contract). Returns a result dict:
      ``{"agent_text", "acked", "replied", "reply_argv", "reason"}``.
    """
    if log is None:
        def log(_msg, *, err=False):
            return None
    if run_reply is None:
        run_reply = _default_run_reply

    event_id = str((evt or {}).get("event_id") or "")

    # (1) dispatch (= inject) → stamp ``opened`` at read-time via the
    #     ``on_dispatched`` seam (= ms-93 / e-3140: ``opened`` is a
    #     read-receipt; the AI reads the DM when it enters the turn, not when
    #     the arbitrarily-long turn ends), then (2) drain until idle.
    #     ``dispatch_dm_and_wait`` may raise → daemon reconnects the transport
    #     for the next DM. A failed inject fires before ``on_dispatched``, so
    #     ``opened`` is not stamped for a DM that never reached a turn.
    rsp = app_server_client.dispatch_dm_and_wait(
        evt, on_dispatched=lambda: ack_fn(event_id)
    )
    agent_text = agent_text_fn((rsp or {}).get("_notifications") or [])

    result = {
        "agent_text": agent_text,
        "acked": True,
        "replied": False,
        "reply_argv": None,
        "reason": "",
    }

    # (4): autonomous reply only when explicitly armed (= e-2519 AC 6 gate).
    if not armed:
        result["reason"] = "not-armed"
        return result

    reply_argv = build_dm_reply_args(
        event=evt, agent_text=agent_text, beacon_bin=beacon_bin,
    )
    result["reply_argv"] = reply_argv
    if reply_argv is None:
        result["reason"] = "reply-skipped-empty-or-unaddressable"
        log(
            f"codex-receive-loop: armed reply skipped event={event_id} "
            "(= empty agent_text or missing event_id / sender_session_id)"
        )
        return result

    try:
        proc = run_reply(reply_argv, sender_session_id=sender_session_id)
    except Exception as exc:  # noqa: BLE001 — best-effort reply contract
        result["reason"] = f"reply-exception:{exc}"
        log(
            f"codex-receive-loop: armed reply exception event={event_id}: {exc}",
            err=True,
        )
        return result

    rc = getattr(proc, "returncode", 1)
    if rc == 0:
        result["replied"] = True
        result["reason"] = "replied"
        log(
            f"codex-receive-loop: armed reply sent event={event_id} "
            f"to={(evt or {}).get('sender_session_id')} (= in_reply_to)"
        )
    else:
        stderr_preview = (
            (getattr(proc, "stderr", "") or "").strip().replace("\n", " ")[:240]
        )
        result["reason"] = f"reply-failed-rc-{rc}"
        log(
            f"codex-receive-loop: armed reply failed event={event_id} "
            f"rc={rc} stderr={stderr_preview!r}",
            err=True,
        )
    return result


__all__ = [
    "ack_event",
    "archive_inbox_event",
    "build_dm_reply_args",
    "dispatch_kept_event",
    "heartbeat_to_server",
    "inbox_dir",
    "list_inbox_events",
    "persist_inbox_event",
    "poll_inbox_once",
]
