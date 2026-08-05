#!/usr/bin/env python3
"""cmd_bus.py — the `beacon bus *` command family (ms-127 e-4803).

Extracted verbatim from commands.py (god-module split). Depends only on
commands_shared (upward) + leaf domain modules, never on commands.py — acyclic
(SPEC 方針4). commands.py re-imports the PUBLIC handlers for dispatch + `commands.X`;
family-private helpers are NOT re-exported (patch them at cmd_bus.<name>).

The budget / recipient / identity leaf helpers this family shares with commands.py
callers (_read_bus_budget / _write_bus_budget / _get_bus_budget_path /
_resolve_recipient_live / _bus_auto_execute_channels /
_mirror_auto_execute_channels_to_local, and the identity/swap helpers those pull
in) were promoted to commands_shared in this same change (e-4803-foundation) so
_arm_for_trek / cmd_acquisition_attack_list_send / _check_recipient_live_health
can keep using them without importing cmd_bus (which would form a cycle).
"""

import json
import os
import sys
import urllib.parse
from typing import Optional

from commands_shared import (
    load_project,
    save_project,
    _resolve_session_id,
    _resolve_bus_project_id,
    _resolve_creator_identity,
    _get_api_client,
    _is_cloud_mode,
    _read_bus_budget,
    _write_bus_budget,
    _get_bus_budget_path,
    _bus_auto_execute_channels,
    _mirror_auto_execute_channels_to_local,
    _resolve_recipient_live,
)


def _bus_resolve_recipient(default_fallback: str = "") -> str:
    """Pick the recipient_id for read-side bus calls.

    Resolution order: CLI flag (BEACON_BUS_RECIPIENT) → current session_id →
    explicit fallback. Hard-error if all three are empty so a typo doesn't
    silently read events for an empty-string recipient that lives next to
    every other empty-string recipient.
    """
    rid = os.environ.get("BEACON_BUS_RECIPIENT", "").strip()
    if rid:
        return rid
    sid = _resolve_session_id()
    if sid:
        return sid
    if default_fallback:
        return default_fallback
    print(
        "bus: no recipient_id. Pass --recipient <id> or run inside a session.",
        file=sys.stderr,
    )
    sys.exit(1)


def _bus_budget_consume_one() -> tuple[bool, dict]:
    """Decrement the budget for a single outbound send.

    Returns (allowed, budget_after). When ``allowed`` is False the caller
    must NOT issue the send. ``budget_after`` is the post-decrement state,
    or the unchanged refuse-state when the budget is exhausted/missing.
    """
    b = _read_bus_budget()
    if b is None:
        # No budget file at all == autonomous mode never armed → allow
        # sends (the gate only applies when armed). Manual one-off DMs from
        # the CLI should not require granting first.
        #
        # INTENTIONAL ASYMMETRY (ms-100 e-3310): the MCP reply gate
        # (channel/bus-budget.mjs consumeBusBudgetOne) REFUSES on a missing
        # file ('not_granted') instead of allowing. CLI = human (allow), MCP =
        # AI (default-OFF, require grant). The two agree on every armed case
        # (total<=0 / exhausted / decrement) and diverge ONLY here, on purpose.
        # Do not "unify" — that either removes armed mode's safety or breaks
        # manual sends. Pinned by tests/test_bus_budget_asymmetry.py; background
        # in SPEC PVaNf6HYFjucgBS3lkQF ("armed の本質").
        return True, {"total": 0, "used": 0, "armed": False}
    total = int(b.get("total", 0))
    used = int(b.get("used", 0))
    if total <= 0:
        return True, {**b, "armed": False}
    if used >= total:
        return False, {**b, "armed": True}
    b["used"] = used + 1
    _write_bus_budget(b)
    return True, {**b, "armed": True}


def _bus_channel_missing_reason(cwd: str) -> Optional[str]:
    """Return a 1-line reason if ``cwd`` has no beacon-bus MCP channel wired,
    else None (ms-120 / e-3899).

    The beacon-bus "channel" (the MCP receive bridge) and the "bus" (the event
    transport) are confusingly-named separate surfaces. When the channel is
    absent, `bus listen` still receives (it polls), but there is no idle-wake
    reception outside that explicit listen — the send-only asymmetry of e-1173.
    Detection mirrors scripts/check-mcp-receive-capability.py detect_status()
    (.mcp.json in cwd + a ``beacon-bus`` entry under ``mcpServers``). Kept as a
    small pure helper so the recovery-hint wiring in cmd_bus_listen is testable.
    """
    mcp_path = os.path.join(cwd, ".mcp.json")
    if not os.path.exists(mcp_path):
        return "この cwd に .mcp.json がありません"
    try:
        with open(mcp_path, "r", encoding="utf-8") as f:
            servers = (json.load(f) or {}).get("mcpServers") or {}
    except (OSError, json.JSONDecodeError):
        return ".mcp.json が読めません (壊れている可能性)"
    if not isinstance(servers, dict) or "beacon-bus" not in servers:
        return ".mcp.json に beacon-bus エントリがありません"
    return None


def _bus_is_armed() -> bool:
    """True iff an auto-reply budget is granted (= autonomous mode active).

    e-3901: "armed" is the durable, human-set signal that this session is in
    autonomous mode — a budget file present with ``total > 0`` (granted via
    ``beacon bus budget grant`` / ``/beacon-bus-armed`` / Trek auto-arm). The
    send gate keys off THIS send-context signal rather than the per-send
    ``--in-reply-to`` flag, so an armed autonomous loop cannot bypass the gate
    by omitting the flag. Mirrors the armed semantics of
    ``_bus_budget_consume_one`` (file present AND total > 0; total<=0 is a
    cleared budget = not armed).
    """
    b = _read_bus_budget()
    return b is not None and int(b.get("total", 0)) > 0


def _bus_budget_refund_one() -> bool:
    """Give back one consumed budget slot (ms-100 e-2999).

    ``_bus_budget_consume_one`` is a *pessimistic* decrement: it commits the
    slot before the send happens so the gate can't be raced. If the send is
    then aborted or fails, that slot was never actually used — refund it so a
    failed send doesn't silently burn an autonomous-reply turn. Clamps ``used``
    at 0 (a double refund / refund-without-consume can't go negative). Returns
    True iff a slot was actually given back. Mirrors
    ``channel/bus-budget.mjs refundBusBudgetOne`` for the MCP path.
    """
    b = _read_bus_budget()
    if b is None:
        return False
    total = int(b.get("total", 0))
    used = int(b.get("used", 0))
    if total <= 0 or used <= 0:
        return False
    b["used"] = used - 1
    _write_bus_budget(b)
    return True


def _record_bus_budget_trek_bypass(trek_id: str) -> None:
    """Bump the per-Trek bypass counter on the budget file (ms-75 / e-2044).

    The counter is informational — it surfaces in ``bus budget show`` so a
    leader can see "this session sent N DMs that bypassed the budget gate
    because they were Trek-internal". It does not gate or rate-limit; the
    structural rationale is that Trek scope IS the gate (= pre-approved
    blanket consent), and a counter gives observability without re-adding
    a soft cap that the SPEC explicitly rejected.

    Best-effort write: budget file absent / unreadable / unwritable all
    silently no-op (= the bypass already happened, audit is the bonus).
    """
    if not trek_id:
        return
    try:
        b = _read_bus_budget() or {
            "total": 0, "used": 0,
            "channels": [], "armed": False,
        }
        bypassed = dict(b.get("trek_bypassed") or {})
        bypassed[trek_id] = int(bypassed.get(trek_id, 0)) + 1
        b["trek_bypassed"] = bypassed
        _write_bus_budget(b)
    except Exception:
        return


def _trek_has_joined_member(
    members: list, *, phase_a: bool, session_id: str, user_id: str
) -> bool:
    """True iff a *joined* member of ``members[]`` identifies (session_id, user_id).

    Branches on the trek's phase, mirroring the server-side ``dm_gate``
    shared-Trek lookup exactly (PR #491 parent re-review). The invited-vs-joined
    distinction is structural, NOT a separate ``joined_at`` check:

      * **phase-A (session_id keyed)**: match by ``session_id`` presence.
        ``trek.accept_invitation`` writes ``session_id`` and ``joined_at``
        together on join, while an invite placeholder has NEITHER — so a member
        carrying a session_id is already joined, and an invite placeholder (no
        session_id) simply never matches a real session id.
      * **pre-A (legacy user_id grain)**: match by ``user_id``. A
        pre-session-grain member predates ``joined_at`` tracking and is joined
        by the legacy contract; imposing ``joined_at`` here would drop existing
        treks (the regression this re-review corrected).
    """
    if phase_a:
        if not session_id:
            return False
        return any((m.get("session_id") or "") == session_id for m in members)
    return bool(user_id) and any(
        (m.get("user_id") or "") == user_id for m in members
    )


def _is_trek_internal_send(recipient_sid: str) -> tuple[bool, str]:
    """Decide whether the current --in-reply-to send is Trek-internal.

    Returns ``(True, trek_id)`` iff both the caller's session and
    ``recipient_sid`` are joined members of the same active Trek; otherwise
    ``(False, "")``.

    Detection material (= mirror of server-side
    ``dm_gate.should_gate_dm_action`` shared_trek_member rule, session grain):
      * caller's user_id + session_id from the cloud identity
      * recipient's user_id resolved from the project session registry
      * caller's joined active treks; both sender session and recipient
        session must appear as joined ``members[]`` of the same trek

    e-4116 (ms-75): the same-user leader↔fork case (= solo-dev dogfood, both
    sessions owned by one cloud user) is the PRIMARY Trek-internal case and
    MUST bypass. The old code excluded ``recipient_user_id == my_user_id``,
    which structurally denied the bypass to exactly that case and made Trek
    coordination deadlock on budget exhaustion. Membership is checked at
    session grain (``_trek_member_matches``), so same-user is included via two
    distinct member entries while a same-user send to a *non-member* session
    is still correctly gated.

    Best-effort: any exception or missing material returns ``(False, "")``
    so the regular budget gate stays in force. The bypass MUST NOT fire
    on a misconfigured send — that would be a silent budget relaxation.
    """
    if not recipient_sid:
        return False, ""
    if not _is_cloud_mode():
        # Local mode uses ~/.beacon/treks/; recipient session lookup
        # requires a cloud-side directory listing. Keep local mode strict
        # (= no bypass), the safer side of the SPEC.
        return False, ""
    try:
        my_user_id, _, my_session_id = _resolve_creator_identity()
        if not my_user_id:
            return False, ""
        client, config = _get_api_client()
        project_id = _resolve_bus_project_id(config)
        # Resolve recipient session → user.
        recipient_user_id = ""
        try:
            sessions = client.list_sessions(project_id) or []
        except Exception:
            return False, ""
        for s in sessions:
            if s.get("session_id") == recipient_sid:
                actor = s.get("actor") or {}
                recipient_user_id = actor.get("user_id") or ""
                break
        # e-4116: NO same-user exclusion. A single user's leader/fork sessions
        # coordinating inside a Trek are the main case to bypass; membership
        # (below) is what gates it, not user distinctness.
        if not recipient_user_id:
            return False, ""
        # Walk my joined active treks. Both my session AND the recipient
        # session must be joined members of the same active trek.
        try:
            my_treks = client.list_treks() or []
        except Exception:
            return False, ""
        for trek in my_treks:
            if trek.get("status") != "active":
                continue
            # e-4116 follow-up (PR #491 parent review 1): a halted trek must
            # NOT grant bypass — leader pulled the Andon cord. This mirrors
            # the server-side dm_gate lookup, which skips halted treks; the
            # CLI mirror was missing it (fork finding M1), so a halted-but-
            # active trek still bypassed budget on the CLI send path.
            if trek.get("halt"):
                continue
            members = trek.get("members") or []
            # Branch on trek phase once (mirror server _is_phase_a_plus), then
            # match both endpoints at that grain. Doing it per-trek (not
            # per-member) is what lets a phase-A invite placeholder — no
            # session_id — be correctly excluded instead of falling through to
            # a legacy user match.
            try:
                import trek as _trek_mod
                phase_a = _trek_mod.is_session_id_keyed(trek)
            except Exception:
                phase_a = False
            am_member = _trek_has_joined_member(
                members, phase_a=phase_a,
                session_id=my_session_id, user_id=my_user_id,
            )
            if not am_member:
                continue
            recipient_is_member = _trek_has_joined_member(
                members, phase_a=phase_a,
                session_id=recipient_sid, user_id=recipient_user_id,
            )
            if recipient_is_member:
                return True, str(trek.get("trek_id") or "")
        return False, ""
    except Exception:
        return False, ""


def cmd_bus_budget_grant():
    """Set or refresh the outbound-send budget for autonomous mode.

    ms-76 / e-1852 structural禁止帯 (= 構造的禁止): budget grant requires a
    human (T1 envelope-equivalent) signal — Operation auto-execute (T2) is
    NOT allowed to self-escalate. The whole point of the budget is to cap
    autonomous-loop runaway. If T2 Operations could re-grant the budget,
    an AI inside a long-running Operation could write a "grant N more
    turns" Operation, schedule itself, and bypass the cap silently. We
    block the path at the CLI entry: if the process is running under
    BEACON_OPERATION_AUTO_EXECUTE=1 (= the Operation runner's marker),
    refuse with a non-zero exit and a message pointing the human to run
    the grant interactively.

    See CORE doc QvyVwRU8otQEn5iMfP36 (= AI 自律 action の envelope tier
    framework) 「構造的禁止」 section. Mirrors the explicit T1-only
    guarantee in ms-76 SPEC EuLwGrAawmMzeKYsxkrd 設計方針 8.
    """
    if os.environ.get("BEACON_OPERATION_AUTO_EXECUTE", "") == "1" or \
       os.environ.get("BEACON_OPERATION_ENVELOPE_ID", "").strip():
        print(
            "Error: bus budget grant is T1-only (= human-signature required).\n"
            "  This process is running under an Operation auto-execute "
            "context (T2 envelope); structural禁止帯 forbids AI self-escalation.\n"
            "  See CORE doc QvyVwRU8otQEn5iMfP36 (= AI 自律 action の envelope "
            "tier framework). Run `beacon bus budget grant --turns N` "
            "interactively (= outside the Operation runner) to refresh.",
            file=sys.stderr,
        )
        sys.exit(1)
    import datetime
    raw = os.environ.get("BEACON_BUS_BUDGET_N", "").strip()
    try:
        total = int(raw)
    except ValueError:
        print(f"Error: --turns must be an integer (got: {raw!r})",
              file=sys.stderr)
        sys.exit(1)
    if total <= 0:
        print("Error: --turns must be > 0 (use `bus budget clear` to revoke)",
              file=sys.stderr)
        sys.exit(1)
    data = {
        "total": total,
        "used": 0,
        "granted_at": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"),
        "channels": [],
    }
    _write_bus_budget(data)
    if os.environ.get("BEACON_JSON", "") == "1":
        print(json.dumps(data, ensure_ascii=False))
    else:
        print(f"Budget granted: {total} outbound sends "
              f"(armed). Use `bus budget show` to inspect.")


def cmd_bus_budget_show():
    b = _read_bus_budget()
    if os.environ.get("BEACON_JSON", "") == "1":
        print(json.dumps(b or {"armed": False}, ensure_ascii=False))
        return
    if b is None:
        print("Budget: not granted (autonomous mode disabled).")
        return
    total = int(b.get("total", 0))
    used = int(b.get("used", 0))
    remaining = max(total - used, 0)
    state = "exhausted (re-grant required)" if total > 0 and used >= total else "armed"
    print(f"Budget: {used}/{total} used  →  {remaining} remaining  ({state})")
    if b.get("granted_at"):
        print(f"  granted_at: {b['granted_at']}")
    # ms-75 / e-2044 — Trek-internal bypass audit. The counter is purely
    # informational; bypassed sends did not consume from total/used. Surface
    # it here so the leader can see "the executor has been chatty within the
    # trek but did not draw down the autonomous-loop guardrail".
    bypassed = b.get("trek_bypassed") or {}
    if bypassed:
        total_bypassed = sum(int(v) for v in bypassed.values())
        print(
            f"  Trek-internal bypassed: {total_bypassed} send(s) "
            f"(did NOT count against budget — ms-75 / e-2044)"
        )
        for trek_id, n in sorted(bypassed.items()):
            print(f"    {trek_id}: {n}")


def cmd_bus_budget_clear():
    """Revoke the budget — disables autonomous outbound sends until re-granted."""
    path = _get_bus_budget_path()
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError as e:
            print(f"Error removing budget file: {e}", file=sys.stderr)
            sys.exit(1)
    print("Budget cleared.")


def cmd_bus_auto_execute_list():
    """Show the channels allowed to auto-execute incoming bus events.

    Empty list (or field absent) ⇒ NO channel is permitted. This is the
    secure default and the CLI prints it explicitly so the user is never
    confused by silence."""
    data = load_project()
    channels = _bus_auto_execute_channels(data)
    if os.environ.get("BEACON_JSON", "") == "1":
        print(json.dumps({"channels": channels}, ensure_ascii=False))
        return
    if not channels:
        print("Bus auto-execute allowlist: (empty) — every auto-execute event "
              "will be downgraded to propose-to-ai.")
        return
    print("Bus auto-execute allowlist:")
    for c in channels:
        print(f"  - {c}")


def cmd_bus_auto_execute_add():
    """Add a channel to the auto-execute allowlist.

    Idempotent: re-adding a channel that's already present is a no-op (no
    duplicate, no error). The list keeps insertion order so audit logs read
    naturally."""
    channel = os.environ.get("BEACON_BUS_AUTO_EXEC_CHANNEL", "").strip()
    if not channel:
        print("Error: --channel <name> required", file=sys.stderr)
        sys.exit(1)
    data = load_project()
    channels = _bus_auto_execute_channels(data)
    if channel in channels:
        if os.environ.get("BEACON_JSON", "") == "1":
            print(json.dumps({"channels": channels, "added": False},
                              ensure_ascii=False))
        else:
            print(f"Channel already in allowlist: {channel}")
        return
    channels.append(channel)
    data["bus_auto_execute_channels"] = channels
    save_project(data, op={
        "op": "bus_auto_execute_add",
        "channel": channel,
    })
    _mirror_auto_execute_channels_to_local(channels)
    if os.environ.get("BEACON_JSON", "") == "1":
        print(json.dumps({"channels": channels, "added": True},
                          ensure_ascii=False))
    else:
        print(f"Added to bus auto-execute allowlist: {channel}")


def cmd_bus_auto_execute_remove():
    """Remove a channel from the auto-execute allowlist.

    Removing a channel that isn't present is also idempotent — the desired
    end state (channel not allowed) is reached either way."""
    channel = os.environ.get("BEACON_BUS_AUTO_EXEC_CHANNEL", "").strip()
    if not channel:
        print("Error: --channel <name> required", file=sys.stderr)
        sys.exit(1)
    data = load_project()
    channels = _bus_auto_execute_channels(data)
    if channel not in channels:
        if os.environ.get("BEACON_JSON", "") == "1":
            print(json.dumps({"channels": channels, "removed": False},
                              ensure_ascii=False))
        else:
            print(f"Channel not in allowlist (no-op): {channel}")
        return
    channels = [c for c in channels if c != channel]
    data["bus_auto_execute_channels"] = channels
    save_project(data, op={
        "op": "bus_auto_execute_remove",
        "channel": channel,
    })
    _mirror_auto_execute_channels_to_local(channels)
    if os.environ.get("BEACON_JSON", "") == "1":
        print(json.dumps({"channels": channels, "removed": True},
                          ensure_ascii=False))
    else:
        print(f"Removed from bus auto-execute allowlist: {channel}")


def cmd_bus_send():
    channel = os.environ.get("BEACON_BUS_CHANNEL", "").strip()
    if not channel:
        print("Error: --channel <name> required", file=sys.stderr)
        sys.exit(1)
    payload_raw = os.environ.get("BEACON_BUS_PAYLOAD", "")
    payload: dict = {}
    if payload_raw:
        try:
            parsed = json.loads(payload_raw)
        except json.JSONDecodeError as e:
            print(f"Error: --payload must be JSON ({e})", file=sys.stderr)
            sys.exit(1)
        if not isinstance(parsed, dict):
            print("Error: --payload must be a JSON object", file=sys.stderr)
            sys.exit(1)
        payload = parsed
    sender = os.environ.get("BEACON_BUS_SENDER", "").strip() or _resolve_session_id()
    delivery = os.environ.get("BEACON_BUS_DELIVERY", "").strip() or "propose-to-ai"
    in_reply_to = os.environ.get("BEACON_BUS_IN_REPLY_TO", "").strip()
    # ms-90 / e-3246: decision-event の背景 (= 直面した問題) と判断理由。
    # DM 発信を「問題駆動の相談」として記録するためのメタデータ。本文とは別に運ぶ。
    dec_context = os.environ.get("BEACON_BUS_CONTEXT", "").strip()
    dec_rationale = os.environ.get("BEACON_BUS_RATIONALE", "").strip()
    # ms-128 / e-4289: idempotent cross-invocation resend. A caller (leader /
    # Skill) that re-runs `beacon bus send` after an ambiguous failure passes
    # --client-event-id <same key> --retry so the server dedups instead of
    # creating a true duplicate DM (the 2026-07-27 dogfood failure). Empty →
    # the client mints a fresh key (normal first send).
    client_event_id = os.environ.get("BEACON_BUS_CLIENT_EVENT_ID", "").strip()
    is_retry = os.environ.get("BEACON_BUS_IS_RETRY", "") == "1"
    # Fail fast (mirror the server's 422) before any network round-trip so the
    # caller gets an actionable message rather than an opaque HTTP error.
    if is_retry and not client_event_id:
        print(
            "Error: --retry requires --client-event-id. Resend with the SAME"
            " client_event_id the first attempt used so the server can dedup"
            " (idempotent send, e-4289). The first send's JSON output carries"
            " it under the \"client_event_id\" key.",
            file=sys.stderr,
        )
        sys.exit(2)
    # e-1209: --to <session_id> stamps payload.recipient_session_id so the
    # server-side filter in /bus/unread can route the event to a single
    # recipient. Without this, `dm`-channel events fan out to every session
    # in the project (the historical bug). MCP reply tool (channel/bus.mjs)
    # already stamps this field; aligning the CLI sender closes the gap.
    #
    # The CLI does NOT require --to for non-DM channels — broadcast remains
    # the default semantics there. For DM the server drops unaddressed events
    # rather than broadcasting, so a `dm` send without --to is effectively
    # a no-op (drops everywhere). We surface a warning rather than hard-error
    # so existing scripts that rely on legacy broadcast behavior get loud
    # feedback instead of silent message loss.
    recipient = os.environ.get("BEACON_BUS_RECIPIENT_SESSION", "").strip()
    # ms-54 / e-2934: user-scoped 宛先 (--to-user)。 session_id churn しても
    # 同 user の別 session が読める、 時差配信 (= 相手が offline でも次回起動時
    # に inbox に届く) を成立させる。 現段階では user_id 直指定のみ受け付ける
    # (= email → user_id 解決は将来 PR で追加、 beacon member list --json で
    # 事前に user_id を引く運用)。
    recipient_user = os.environ.get("BEACON_BUS_RECIPIENT_USER", "").strip()
    if recipient and recipient_user:
        # bash 側でも弾いているが python 側でも念のため hard error
        # (= 直接 python3 commands.py bus_send を叩かれても排他を守る)。
        print(
            "Error: --to (session-scoped) と --to-user (user-scoped) は相互排他"
            " です。 どちらか片方のみ指定してください。",
            file=sys.stderr,
        )
        sys.exit(1)
    if recipient_user and "@" in recipient_user:
        # 現段階では email 解決 未実装 (= 別 PR で実装予定)。 user_id を
        # 使うよう案内する親切エラー。
        print(
            "Error: --to-user は現在 user_id のみ受け付けます (email 解決は"
            " 未実装)。 `beacon member list --json` で受信者の user_id を"
            " 引いて指定してください。",
            file=sys.stderr,
        )
        sys.exit(1)
    if recipient:
        # Caller-supplied --to overrides any payload.recipient_session_id
        # set by --payload; the flag is the unambiguous source of truth.
        payload = {**payload, "recipient_session_id": recipient}
    elif recipient_user:
        # user-scoped 宛先: payload.recipient_user_id を stamp。
        # session_id 経路とは別 field なので、 server 側 filter (= app.py
        # _bus_event_addressed_to) で 「recipient_session_id が空 かつ
        # recipient_user_id が set」 として認識される。
        payload = {**payload, "recipient_user_id": recipient_user}
    elif channel == "dm" and not payload.get("recipient_session_id") \
            and not payload.get("recipient_user_id"):
        print(
            "Warning: sending to channel 'dm' without --to <session_id> or "
            "--to-user <user_id>. After e-1209 the server drops unaddressed "
            "DM events rather than broadcasting them — pass --to or --to-user "
            "or use a different channel for broadcasts.",
            file=sys.stderr,
        )

    # e-1000 / e-3901: autonomous-send gate.
    #
    # POLARITY (e-3901): the gate triggers on the *autonomous context* of the
    # send, NOT on the presence of `--in-reply-to`. Before e-3901 the gate
    # fired ONLY when `--in-reply-to` was set, so an armed autonomous loop
    # could bypass BOTH the budget cap AND the cross-user HOLD by simply
    # omitting the flag — a safety boundary broken by a missing flag is a
    # prompt-level ("please pass the flag") constraint, exactly what AX
    # principle 6 forbids. The inbox-hook instruction to "always pass the
    # flag" was the sole thing standing between an autonomous loop and an
    # ungated send.
    #
    # A send is "autonomous" when EITHER:
    #   * `--in-reply-to` is set (an explicit reply — preserves the historical
    #     default-OFF behaviour: a reply with no granted budget is REFUSED), OR
    #   * the session is armed (a budget was granted ⇒ autonomous mode is
    #     active) — THIS branch closes the omit-the-flag hole: an armed loop is
    #     gated regardless of whether it remembered `--in-reply-to`.
    # The gate is suppressed ONLY by an explicit, audited `--manual` override:
    # a human hand-composing a send while armed IS the approval (recorded on
    # the payload for the decision-event trail). Manual sends from a
    # non-armed session need nothing — the human typing the command is the
    # approval, same as before.
    #
    # When the gate applies:
    #   * no budget granted at all (autonomous only via --in-reply-to) → REFUSE.
    #   * budget granted but exhausted → REFUSE. The human must re-grant.
    #   * budget granted and remaining > 0 → decrement, then send. The
    #     decrement happens *before* the cloud call so a network failure
    #     can't smuggle an extra send past the gate. If the send then fails
    #     the slot is refunded (e-2999) so a failed send doesn't burn a turn.
    manual = os.environ.get("BEACON_BUS_MANUAL", "").strip() == "1"
    armed = _bus_is_armed()
    autonomous = bool(in_reply_to) or armed
    gate_applies = autonomous and not manual

    budget: dict = {"armed": False}
    # ms-100 e-2999: True once a real budget slot was decremented (not a Trek
    # bypass), so a failed send below can refund exactly that slot.
    _budget_slot_consumed = False
    if gate_applies:
        # ms-75 / e-2044: Trek-internal sends bypass the budget gate. Trek
        # is an opt-in pre-approval scope (= 缶詰の作業部屋), so the budget
        # cap (= runaway-autonomy guardrail) is structurally redundant for
        # member-to-member DMs within an active Trek both sides have
        # joined. server/dm_gate.should_gate_dm_action already short-
        # circuits the receiver-side cross-user gate for shared_trek_member
        # pairs (ms-70 / e-1854); this client-side check brings the budget
        # layer into parity so a Trek doesn't deadlock on budget exhaustion
        # while leader/executor DMs keep flowing under blanket consent.
        # The check is best-effort: any error path (no auth, network blip,
        # unknown recipient) falls through to the regular budget gate so we
        # never silently relax enforcement on a misconfigured send.
        trek_bypass, bypass_trek_id = _is_trek_internal_send(recipient)
        if trek_bypass:
            budget = {
                "armed": True,
                "trek_bypass": True,
                "trek_id": bypass_trek_id,
            }
            _record_bus_budget_trek_bypass(bypass_trek_id)
        else:
            b = _read_bus_budget()
            if b is None:
                # Reachable only when autonomous via --in-reply-to but not armed
                # (no budget file): _bus_is_armed() guarantees a file when armed.
                # Preserves the historical default-OFF: a reply needs a grant.
                print(
                    "Error: this is an autonomous send (--in-reply-to set) but no "
                    "auto-reply budget is granted. Default state requires human "
                    "approval. Run `beacon bus budget grant <N>` to authorize N "
                    "auto-sends, or pass --manual for an explicit human-approved "
                    "one-off send.",
                    file=sys.stderr,
                )
                sys.exit(1)
            allowed, budget = _bus_budget_consume_one()
            if not allowed:
                total = int(budget.get("total", 0))
                used = int(budget.get("used", 0))
                print(
                    f"Error: auto-reply budget exhausted ({used}/{total} used). "
                    "Run `beacon bus budget grant <N>` to re-grant, or pass "
                    "--manual for an explicit human-approved one-off send.",
                    file=sys.stderr,
                )
                sys.exit(1)
            # A real slot was decremented (armed budget, not a Trek bypass).
            # Remember so the send failure path can refund it (e-2999).
            _budget_slot_consumed = bool(budget.get("armed"))
    elif manual and armed:
        # e-3901: an explicit --manual override of the armed gate. Stamp the
        # payload so the decision-event trail shows a human deliberately
        # bypassed the autonomous gate (audit), and surface a one-line note.
        payload = {**payload, "manual_override": True}
        print(
            "Note: --manual override — this send bypasses the armed auto-reply "
            "gate (recorded as a human-approved override).",
            file=sys.stderr,
        )

    if in_reply_to:
        # Thread the reply by stamping the parent event_id on the payload.
        # Server-side schema treats payload as opaque; this is a convention
        # the inbox hook can present back to the AI on subsequent rounds.
        payload = {**payload, "in_reply_to": in_reply_to}

    client, config = _get_api_client()
    project_id = _resolve_bus_project_id(config)  # e-1151: --project override

    # ms-94 / e-2811 (2026-07-06): sender の真 home project を payload に stamp。
    # 従来は bridge (channel/bus.mjs) が payload.source_project を fallback で
    # PROJECT_ID (= receiver bridge の own project_id) に埋めていたため、 event
    # の from_project は 「送信元」 ではなく 「受信 bridge の cwd project」 に
    # なっていた (= audit 不能 / cross-project reply が壊れる)。 sender の cwd
    # project (= cloud.json.project_id、 --project override では変わらない
    # 「本人のいる project」) を明示 stamp して bridge fallback を使わせない。
    #
    # 順序: (1) 既に payload.source_project が set されていればそれを尊重、
    # (2) cwd cloud.json.project_id (= 本人の home、 --project override 前提)、
    # (3) 最終 fallback として現在の target project_id (= --project override 後)。
    if not payload.get("source_project"):
        cwd_project_id = str((config or {}).get("project_id") or "").strip()
        source_project = cwd_project_id or project_id
        if source_project:
            payload = {**payload, "source_project": source_project}
    # e-1340 follow-up (= structural defense against the 2026-06-09 silent
    # cross-project misroute incident): if --to points at a session that
    # polls a different project than `project_id`, auto-route to that
    # project. Loud stderr notice keeps the override auditable.
    project_id = _validate_recipient_project(recipient, project_id, channel)
    # e-1402 + e-2280: liveness gate on the sid itself, with conservative
    # auto-swap when the stale sid maps to a single live healthy sibling
    # owned by the same user. ms-95 / e-2280 lifts AI's per-send duty to
    # manually re-resolve via `bus directory --live` after observed
    # 2026-06-22 kyozai dolphin Trek dogfood (= 2 stale sends in 30 min).
    swapped_recipient, swap_notice, _recipient_email = _resolve_recipient_live(
        recipient, channel)
    if swap_notice is not None and swapped_recipient != recipient:
        # Update payload + re-validate project routing for the new sid.
        # The new session may live in a different project (= different
        # bclaude worktree); _validate_recipient_project handles the
        # cross-project hop with its own stderr notice.
        recipient = swapped_recipient
        payload = {**payload, "recipient_session_id": recipient}
        project_id = _validate_recipient_project(recipient, project_id, channel)

    # e-1290: envelope-by-default for CLI sends.
    #
    # Tier selection rule: `beacon bus send` is invoked by a human typing a
    # command in their shell, so every send carries an implicit human
    # signature → tier T1. `--action <name>` (repeatable, comma-joined by the
    # bash wrapper) populates actions_authorized; with no flag the list is
    # empty (T1 with no specific delegations is still a valid envelope and
    # unlocks `propose-to-ai` delivery on the receive side).
    #
    # Fallback strategy:
    #   * `--no-envelope` (BEACON_BUS_NO_ENVELOPE=1) skips issuance entirely.
    #     Useful for debugging the server's legacy / backward-compat path.
    #   * Issuance returns HTTP 404 (older server without /bus/envelope/issue)
    #     → log + fall back to legacy POST. This keeps newer clients deployable
    #     against unmigrated servers.
    #   * Issuance returns HTTP 400 (server rejected our request — e.g. a
    #     high-risk action under a tier the server forbids, or a wildcard)
    #     → log + fall back. The user still gets their message through; the
    #     receiver simply won't auto-execute, which is the safe default.
    #   * Transport / 5xx errors also fall through silently — bus must not
    #     break for the user just because the envelope path misbehaves.
    envelope_obj: dict | None = None
    requested_action: str | None = None
    no_envelope = os.environ.get("BEACON_BUS_NO_ENVELOPE", "") == "1"

    # ms-110 / e-3445: build the recipient_confirmed consent claim BEFORE the
    # envelope is minted, so it can be passed INTO the mint call and signed in.
    # The claim used to be grafted onto the already-signed envelope afterward
    # (see the removed block below), which invalidated the signature → the
    # receive-time verify degraded the DM to T5 → 403 on every
    # --recipient-confirmed send. Building it up-front and handing it to the
    # server mint keeps the claim authentic (server HMAC covers it).
    #
    # The claim is required for a cross-user DM (server backstop e-3443); the
    # /beacon-dm-send Skill sets BEACON_BUS_RECIPIENT_CONFIRMED=1 only after its
    # human-confirmation step. A raw primitive call (no Skill, no flag) carries
    # no claim and is rejected server-side — that is the whole point.
    _consent_claim: dict | None = None
    if os.environ.get("BEACON_BUS_RECIPIENT_CONFIRMED", "") == "1":
        import dm_consent
        try:
            _cid_user, _cid_email, _ = _resolve_creator_identity()
        except Exception:
            _cid_user, _cid_email = "", ""
        if not _cid_user:
            print(
                "Error: --recipient-confirmed requires a resolvable sender"
                " identity (could not determine your user_id).",
                file=sys.stderr,
            )
            sys.exit(1)
        if no_envelope:
            # No envelope will be minted (--no-envelope), so there is nothing to
            # carry the claim. A cross-user send would be rejected server-side
            # without it, so fail loudly rather than posting something that 403s.
            print(
                "Error: --recipient-confirmed needs an envelope to carry the"
                " confirmation, but --no-envelope was set. Retry without"
                " --no-envelope.",
                file=sys.stderr,
            )
            sys.exit(1)
        try:
            _consent_claim = dm_consent.build_recipient_confirmed_claim(
                confirmed_by_user_id=_cid_user,
                confirmed_by_email=_cid_email or "",
                recipient_session_id=recipient or "",
                recipient_user_id=recipient_user or "",
                recipient_project_id=project_id or "",
                channel=channel,
            )
        except ValueError as e:
            print(
                f"Error: cannot build recipient confirmation ({e}).",
                file=sys.stderr,
            )
            sys.exit(1)

    if not no_envelope:
        actions_raw = os.environ.get("BEACON_BUS_ACTION", "").strip()
        actions_authorized = [
            a.strip() for a in actions_raw.split(",") if a.strip()
        ] if actions_raw else []
        # When the user passes a single --action we forward it as the
        # message's requested_action too; the envelope's actions_authorized
        # is the *capability grant* (what the receiver may auto-run), and
        # requested_action is the *request* (what this message is asking
        # for). For T1 these line up; for the wider model they need not.
        if len(actions_authorized) == 1:
            requested_action = actions_authorized[0]
        try:
            # Pass consent_claim only when present so the ordinary send path
            # calls issue_bus_envelope with the exact prior kwargs (keeps the
            # legacy fallback + existing test stubs unchanged). Only the
            # --recipient-confirmed path adds the extra kwarg.
            _issue_kwargs = {}
            if _consent_claim is not None:
                _issue_kwargs["consent_claim"] = _consent_claim
            envelope_obj = client.issue_bus_envelope(
                project_id,
                tier="T1",
                actions_authorized=actions_authorized,
                data_class="free",
                **_issue_kwargs,
            )
        except RuntimeError as e:
            # api_client wraps HTTPError as `RuntimeError("API error CODE: ...")`.
            # 404 = legacy server, 400 = rejected payload — both fall back to
            # the legacy POST path. Any other shape (transport, 5xx) also
            # falls through; we log but don't surface so the user's send
            # still lands.
            msg = str(e)
            if "API error 404" in msg or "API error 400" in msg:
                print(
                    f"Note: envelope issuance unavailable ({msg.split(':', 1)[0]});"
                    " falling back to legacy bus path.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"Note: envelope issuance error, falling back to legacy"
                    f" bus path: {msg}",
                    file=sys.stderr,
                )
            envelope_obj = None
            requested_action = None
        except (ConnectionError, OSError) as e:
            print(
                f"Note: envelope issuance network error, falling back to"
                f" legacy bus path: {e}",
                file=sys.stderr,
            )
            envelope_obj = None
            requested_action = None

    # ms-110 / e-3445: the recipient_confirmed consent claim (built above) was
    # passed INTO the mint call and signed in by the server, so it now rides on
    # ``envelope_obj`` as a signed field. It is NO LONGER grafted here after
    # signing — doing so invalidated the signature and 403'd every cross-user
    # send (the bug this fix closes).
    #
    # Guard: if the claim was requested but the mint fell back to the legacy
    # path (issuance 404/400/transport error → envelope_obj is None), there is
    # no signed envelope to carry the claim. A cross-user send would be rejected
    # server-side without it, so fail loudly rather than posting a bare event
    # that will 403 with a confusing reason.
    if _consent_claim is not None and envelope_obj is None:
        print(
            "Error: --recipient-confirmed needs a signed envelope to carry the"
            " confirmation, but envelope issuance failed (server fell back to"
            " the legacy path). The cross-user send would be rejected. Retry"
            " once envelope issuance is available.",
            file=sys.stderr,
        )
        sys.exit(1)

    # ms-90 / e-3246: 相談を「開始」する DM (= 返信でない) で背景 (context) が
    # 空なら、書くよう促す (= SPEC §設計方針3: promote、hard block しない)。
    # 返信 (in_reply_to あり) は継続なので促さない (= ノイズ回避、主役は開始の瞬間)。
    if channel == "dm" and not in_reply_to and not dec_context:
        print(
            "Note: 背景なしで DM を発信します。この相談で『どんな問題に直面したか』を"
            " --context \"...\" で添えると、意思決定の記録 (decision-event) が"
            " 問題駆動の相談として残ります (任意、送信は止めません)。",
            file=sys.stderr,
        )

    # Qualitative gate (ms-93 e-3340 — mirrors channel/bus-qualgate.mjs).
    # When a real budget slot was consumed (= this is an armed autonomous send,
    # e-3901 no longer requires --in-reply-to to reach here), HOLD 外部宛 / 機密
    # / action付き sends for the human instead of letting them go out. This
    # brings the CLI send path — how a Codex armed session replies
    # (codex_receive_loop builds `beacon bus send`) — to parity with bus.mjs's
    # MCP reply gate, so e-3308's qualitative safety isn't Claude-only. A
    # consumed slot implies gate_applies was True (Trek bypass / --manual never
    # set it). Held sends refund the slot (e-2999). BEACON_QUALGATE_OFF=1 opts
    # out (same escape hatch as the JS side).
    if (
        _budget_slot_consumed
        and os.environ.get("BEACON_QUALGATE_OFF") != "1"
    ):
        import dm_qualgate
        _sender_proj = str((config or {}).get("project_id") or "").strip()
        _actions = [
            a for a in os.environ.get("BEACON_BUS_ACTION", "").split(",")
            if a.strip()
        ]
        # e-3566: pass sender + recipient identity (email) so a same-user
        # cross-project DM is not over-blocked as 外部宛. Sender email comes from
        # the login credentials; recipient email from the live check above (both
        # reuse identity we already resolve — no extra network call).
        try:
            _, _sender_email, _ = _resolve_creator_identity()
        except Exception:
            _sender_email = ""
        _cat = dm_qualgate.classify_outbound_reply(
            channel=channel,
            sender_project_id=_sender_proj,
            recipient_project_id=project_id,
            actions_authorized=_actions,
            confidential=False,
            sender_user_id=_sender_email,
            recipient_user_id=_recipient_email,
        )
        _q = dm_qualgate.evaluate_outbound_qual_gate(_cat, armed=True)
        if _q["hold"]:
            # Held, not delivered → refund the consumed slot so a held reply
            # doesn't cost an autonomous turn.
            _bus_budget_refund_one()
            print(dm_qualgate.qual_hold_message(_q["category"]), file=sys.stderr)
            sys.exit(1)

    try:
        event = client.post_bus_event(
            project_id, channel,
            sender_session_id=sender,
            payload=payload,
            delivery=delivery,
            envelope=envelope_obj,
            requested_action=requested_action,
            context=dec_context,
            rationale=dec_rationale,
            client_event_id=client_event_id,
            is_retry=is_retry,
        )
    except BaseException:
        # The send failed — refund the pessimistically-decremented slot
        # (e-2999) so a network / server error doesn't silently burn an
        # autonomous-reply turn. Then re-raise so the failure still surfaces.
        if _budget_slot_consumed:
            _bus_budget_refund_one()
        raise
    if os.environ.get("BEACON_JSON", "") == "1":
        # Augment the event JSON with the post-decrement budget so scripted
        # callers can decide whether to keep the autonomous loop running.
        out = dict(event)
        if budget.get("armed"):
            out["_budget"] = {
                "total": int(budget.get("total", 0)),
                "used": int(budget.get("used", 0)),
                "remaining": max(int(budget.get("total", 0))
                                  - int(budget.get("used", 0)), 0),
            }
        print(json.dumps(out, ensure_ascii=False))
        return
    line = (
        f"Sent: [{event.get('event_id', '?')}] {channel} "
        f"from={sender or '(anonymous)'} delivery={event.get('delivery', delivery)}"
    )
    if budget.get("armed"):
        total = int(budget.get("total", 0))
        used = int(budget.get("used", 0))
        remaining = max(total - used, 0)
        line += f"  (budget: {used}/{total}, {remaining} remaining)"
    print(line)


def _fetch_pending_dm_lookup(client, project_id: str) -> dict:
    """Fetch the project-scoped pending sidecar map for inline banner emission.

    Used by ``cmd_bus_listen`` / ``cmd_bus_receive`` (ms-70 / e-1715) to
    decide whether to print a banner before each event. Returns
    ``{event_id: row}`` (= via ``lib/dm_pending.build_pending_lookup``).

    All failures (= cloud unconfigured, endpoint absent on a server
    older than e-1714, transient network blip, auth missing) collapse
    to an empty dict — the CLI's existing event-stream output keeps
    flowing untouched. This is the explicit AC requirement: sidecar
    lookup failure MUST NOT break legacy listen / receive behaviour.

    We deliberately do NOT pass ``receiver_user_id`` because resolving
    the local user_id at this point would require touching the auth
    layer (= ``.beacon/cloud.json`` / ``~/.beacon/auth.json``) which
    has multiple resolution orders. The endpoint is membership-gated,
    so the empty receiver_user_id case returns all project-scoped
    pending rows; the banner only fires for events that actually appear
    in *this* recipient's unread feed, which is the natural filter.
    """
    try:
        from dm_pending import build_pending_lookup
    except Exception:
        return {}
    try:
        rows = client.get(
            f"/api/projects/{project_id}/dm/pending"
            f"?receiver_user_id=&limit=200"
        )
    except Exception:
        # Silent: this includes 404 (older server), 401 (no token),
        # 5xx (transient), and OSError (no network).
        return {}
    if not isinstance(rows, list):
        return {}
    try:
        return build_pending_lookup(rows)
    except Exception:
        return {}


def _print_event_with_banner(ev: dict, pending_lookup: dict) -> None:
    """Print a single bus event JSON line, optionally preceded by the
    pending-DM banner.

    ms-70 / e-1715: if ``ev['event_id']`` is in ``pending_lookup`` (= the
    dispatcher gate set ``approval_status="pending"`` for this envelope),
    emit a structurally recognizable banner line first so the AI session
    consuming the stream stops and asks the user before acting on the
    envelope's actions_authorized.

    Banner formatting / contract lives in ``lib/dm_pending.py``. This
    function is intentionally a thin wrapper so the legacy "just print
    JSON" behaviour stays one line away in a diff.
    """
    if pending_lookup:
        eid = ev.get("event_id")
        if eid and eid in pending_lookup:
            try:
                from dm_pending import format_inline_dm_banner
                row = pending_lookup[eid]
                banner = format_inline_dm_banner(
                    eid,
                    sender_user_id=row.get("sender_user_id", ""),
                    created_at=row.get("created_at", ""),
                )
                print(banner, flush=True)
            except Exception:
                # If formatting fails for any reason, fall back to the
                # plain JSON line. The AC says "do not break legacy
                # output"; missing banner is worse than missing JSON.
                pass
    print(json.dumps(ev, ensure_ascii=False), flush=True)


def cmd_bus_listen():
    """Long-poll /bus/unread and stream each event as one JSON line on stdout.

    Designed for Claude Code's Monitor tool: each stdout line becomes one
    notification. Without explicit acknowledgement (``--auto-ack``) events
    stay readable so a crashing consumer can replay; with auto-ack, the
    cursor advances to the last event of each batch after print.

    The loop ends only on SIGINT or when the optional ``--once`` mode has
    delivered a batch. There is no implicit timeout — callers wanting one
    should use ``beacon bus receive --timeout`` instead.

    ms-70 / e-1715: before printing each event, look up its sidecar
    ``approval_status``. If pending, emit an inline banner so the AI
    session reading the stream stops and asks the user before acting.
    Sidecar lookup failures (= older server / no auth / network) silently
    fall back to the legacy "just JSON" output — the banner is added on
    top of the existing contract, not in place of it.

    ms-95 / e-1454: wrap the per-iteration ``list_unread_bus_events``
    call in a transient-network retry shield. The Monitor armed-mode
    flow (= ``/beacon-bus-armed``) depends on this process staying
    alive across SSL handshake timeouts / fetch failures / connection
    resets. On a network exception we log to stderr, sleep with
    exponential backoff (1 → 2 → 4 → 8 → 16 → 30s cap), and continue
    the loop; on a successful round-trip the backoff resets to 1s.
    Logic errors (= TypeError, KeyError, etc.) still propagate.
    """
    import socket
    import ssl
    import time
    import urllib.error
    recipient = _bus_resolve_recipient()
    channel = os.environ.get("BEACON_BUS_CHANNEL", "").strip()
    auto_ack = os.environ.get("BEACON_BUS_AUTO_ACK", "") == "1"
    once = os.environ.get("BEACON_BUS_ONCE", "") == "1"
    interval = float(os.environ.get("BEACON_BUS_INTERVAL", "2") or "2")
    if interval < 0.25:
        interval = 0.25  # don't hammer the server

    client, config = _get_api_client()
    project_id = _resolve_bus_project_id(config)  # e-1151: --project override

    # ms-95 / e-1454: backoff state for transient network errors. Doubles
    # on each consecutive failure up to ``backoff_cap``, resets to 1 on
    # the next successful network round-trip. Function-local int keeps
    # the contract simple — no module state, no test fixture juggling.
    backoff_seconds = 1
    backoff_cap = 30

    # ms-120 / e-3899: recovery hint for the send-only asymmetry (e-1173). The
    # "channel" (the beacon-bus MCP receive bridge) and the "bus" (the event
    # transport) are different surfaces with confusingly similar names. If this
    # cwd has no beacon-bus channel wired, `bus listen` still streams here (it
    # polls directly), but stopping it leaves NO idle-wake reception — a
    # context-free operator can't derive that from the silence. Surface the
    # relationship + recovery path once at startup. Skipped for --once (a
    # transient peek, e.g. /beacon-dm-send reply mode, shouldn't nag). Any
    # failure is swallowed so this never blocks listen.
    if not once:
        try:
            _chan_reason = _bus_channel_missing_reason(os.getcwd())
            if _chan_reason:
                print(
                    "[bus listen] Note: beacon-bus channel が未設置です "
                    f"({_chan_reason})。この explicit listen は動きますが、"
                    "止めると他セッションからの DM で自動起床 (idle-wake) しません "
                    "(= 送信専用の非対称、e-1173)。回復: この cwd で "
                    "`beacon channel install` を実行してください。",
                    file=sys.stderr,
                    flush=True,
                )
        except Exception:
            pass

    try:
        while True:
            try:
                events = client.list_unread_bus_events(
                    project_id, recipient, channel=channel,
                )
            except (urllib.error.URLError, ssl.SSLError,
                    TimeoutError, socket.timeout) as net_err:
                # Transient network error: log to stderr (so an attached
                # human / debug tail can see the cause), sleep with
                # exponential backoff, then continue. Do NOT raise — the
                # Monitor armed-mode loop depends on the process staying
                # alive across SSL handshake timeouts / fetch failures /
                # connection resets (2026-06-11 02:00Z incident).
                print(
                    f"[bus listen] transient network error: "
                    f"{type(net_err).__name__}: {net_err} "
                    f"(retry in {backoff_seconds}s)",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, backoff_cap)
                continue
            # Successful round-trip: reset backoff so the next failure
            # starts at 1s again (= short hiccups don't compound).
            backoff_seconds = 1
            if events:
                # e-1715: refresh sidecar lookup once per batch so a
                # newly-approved row in the middle of a poll loop stops
                # banner emission on the very next poll. Per-event
                # lookups would be more accurate but multiply HTTP cost
                # by N — batch granularity is the right tradeoff.
                pending_lookup = _fetch_pending_dm_lookup(client, project_id)
                for ev in events:
                    _print_event_with_banner(ev, pending_lookup)
                if auto_ack:
                    last_ts = events[-1].get("created_at", "")
                    if last_ts:
                        client.advance_bus_cursor(project_id, recipient, last_ts)
                if once:
                    return
            time.sleep(interval)
    except KeyboardInterrupt:
        return


def cmd_bus_receive():
    """Block until a single batch of events arrives (or ``--timeout`` elapses).

    ms-70 / e-1715: same inline pending-DM banner injection as
    ``cmd_bus_listen``. ``beacon bus receive`` is the one-shot variant
    used by ``beacon-bus-inbox-hook.py`` and ad-hoc scripts; treating
    its output identically keeps the AI gate contract (= banner-then-event)
    uniform across the two CLI entry points.
    """
    import time
    recipient = _bus_resolve_recipient()
    channel = os.environ.get("BEACON_BUS_CHANNEL", "").strip()
    auto_ack = os.environ.get("BEACON_BUS_AUTO_ACK", "") == "1"
    timeout_raw = os.environ.get("BEACON_BUS_TIMEOUT", "0")
    try:
        timeout = float(timeout_raw or "0")
    except ValueError:
        timeout = 0.0
    interval = float(os.environ.get("BEACON_BUS_INTERVAL", "2") or "2")
    if interval < 0.25:
        interval = 0.25

    client, config = _get_api_client()
    project_id = _resolve_bus_project_id(config)  # e-1151: --project override

    started = time.monotonic()
    while True:
        events = client.list_unread_bus_events(
            project_id, recipient, channel=channel,
        )
        if events:
            pending_lookup = _fetch_pending_dm_lookup(client, project_id)
            for ev in events:
                _print_event_with_banner(ev, pending_lookup)
            if auto_ack:
                last_ts = events[-1].get("created_at", "")
                if last_ts:
                    client.advance_bus_cursor(project_id, recipient, last_ts)
            return
        if timeout > 0 and (time.monotonic() - started) >= timeout:
            # Exit 2 distinguishes "timeout, no events" from "error" (1) and
            # "got events" (0) so a calling script can branch on the outcome.
            sys.exit(2)
        time.sleep(interval)


def cmd_bus_ack():
    """Advance the recipient's cursor explicitly. Forward-only — older values
    are silent no-ops on the server side.

    Two input modes (exactly one required):
      * ``--last-seen-at <iso8601>``  Pass the cursor target directly.
      * ``--event <event_id>``        Look up the event server-side and
        advance the cursor past its ``created_at``. Used by
        ``/beacon-operation-execute`` as a forcing function (e-1423, ms-54
        Bug 4 第 2 層): Skill completion structurally clears the triggering
        event from the next inbox-hook inject, so the same op-X event is
        never replayed even if the session_id rotates (= Bug 5 root cause).
    """
    recipient = _bus_resolve_recipient()
    last_seen_at = os.environ.get("BEACON_BUS_LAST_SEEN_AT", "").strip()
    event_id = os.environ.get("BEACON_BUS_ACK_EVENT_ID", "").strip()

    if last_seen_at and event_id:
        print("Error: pass either --last-seen-at or --event, not both",
              file=sys.stderr)
        sys.exit(1)
    if not last_seen_at and not event_id:
        print("Error: --last-seen-at <iso8601> or --event <event_id> required",
              file=sys.stderr)
        sys.exit(1)

    client, config = _get_api_client()
    project_id = _resolve_bus_project_id(config)  # e-1151: --project override

    if event_id:
        # Resolve created_at from the server. The event row carries the
        # canonical timestamp, so the Skill never has to format ISO8601
        # itself (= fewer ways to get the ack wrong).
        try:
            event = client.get_bus_event(project_id, event_id)
        except Exception as e:
            msg = str(e)
            if "404" in msg:
                print(f"Error: bus event {event_id!r} not found in project "
                      f"{project_id!r}", file=sys.stderr)
                sys.exit(1)
            print(f"Error: {msg}", file=sys.stderr)
            sys.exit(1)
        last_seen_at = (event or {}).get("created_at", "").strip()
        if not last_seen_at:
            print(f"Error: bus event {event_id!r} has no created_at "
                  f"(cannot derive cursor target)", file=sys.stderr)
            sys.exit(1)

    result = client.advance_bus_cursor(project_id, recipient, last_seen_at)
    if os.environ.get("BEACON_JSON", "") == "1":
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"Cursor: {recipient} → {result.get('last_seen_at', '(unchanged)')}")


def cmd_bus_status():
    """Render the 3-stage receipt view for a single bus event (ms-54 / e-1348).

    `sent` is always present (the row exists ⇒ the server stamped created_at).
    `delivered` and `opened` are set by channel/bus.mjs receipts and may be
    absent — the renderer marks them as "(not yet)" so the sender can localize
    exactly where a DM stalled:

      * sent ✓ / delivered ✗ / opened ✗ → bridge never polled / event filtered
        before /unread returned it (e.g. cursor already past it)
      * sent ✓ / delivered ✓ / opened ✗ → bridge fetched but the event got
        dropped at the filter chain (channel allowlist, self-sent, mis-addressed)
      * sent ✓ / delivered ✓ / opened ✓ → harness saw the channel push; the AI
        has structurally seen the content

    JSON mode emits the raw event dict — the same shape callers get from
    GET /api/projects/{id}/bus/{event_id} — so scripts can pivot on the
    receipt fields directly.
    """
    event_id = os.environ.get("BEACON_BUS_EVENT_ID", "").strip()
    if not event_id:
        print("Usage: beacon bus status <event_id> [--project <id>] [--json]",
              file=sys.stderr)
        sys.exit(2)
    client, config = _get_api_client()
    project_id = _resolve_bus_project_id(config)
    try:
        event = client.get_bus_event(project_id, event_id)
    except Exception as e:
        # api_client raises a plain Exception with the HTTP status message.
        # 404 is the common case (typo / event GC'd); surface it as a single
        # line rather than a Python traceback.
        msg = str(e)
        if "404" in msg:
            print(f"Error: bus event {event_id!r} not found in project "
                  f"{project_id!r}", file=sys.stderr)
            sys.exit(1)
        print(f"Error: {msg}", file=sys.stderr)
        sys.exit(1)

    if os.environ.get("BEACON_JSON", "") == "1":
        print(json.dumps(event, ensure_ascii=False))
        return

    # Human view. Stage rows align so a quick eye-scan over multiple events
    # locks onto the missing checkmark.
    sent_at = event.get("created_at", "")
    delivered_at = event.get("delivered_at", "")
    delivered_by = event.get("delivered_by", "")
    opened_at = event.get("opened_at", "")
    opened_by = event.get("opened_by", "")

    def _fmt_by(sid, identity):
        """Prefer the Phase 3 resolved identity (ms-93) over the raw sid so a
        human can tell WHO opened / delivered a DM. Falls back to the sid when
        the server did not attach attribution (legacy event, GC'd session, or
        third-party caller whose view is gated)."""
        if isinstance(identity, dict) and identity:
            email = identity.get("email") or ""
            attrs = [
                identity.get("machine") or "",
                identity.get("agent_kind") or "",
                identity.get("cwd") or "",
            ]
            attrs = [a for a in attrs if a]
            label = email or (sid or "")
            if attrs:
                label = f"{label} [{' / '.join(attrs)}]"
            return label
        return sid or ""

    def _row(mark, label, ts, by=""):
        ts_str = ts if ts else "(not yet)"
        by_str = f"  by {by}" if by else ""
        return f"  {mark} {label:<10} {ts_str}{by_str}"

    channel = event.get("channel", "")
    delivery = event.get("delivery", "")
    sender = event.get("sender_session_id", "")
    sender_by = _fmt_by(sender, event.get("sender_identity"))
    payload = event.get("payload", {})

    print(f"event: {event_id}")
    print(f"  channel: {channel}  delivery: {delivery}")
    print(f"  sender:  {sender_by}")
    try:
        print(f"  payload: {json.dumps(payload, ensure_ascii=False)}")
    except Exception:
        print(f"  payload: {payload!r}")
    print("  receipt:")
    print(_row("✓", "sent", sent_at))
    print(_row("✓" if delivered_at else "✗", "delivered", delivered_at,
               _fmt_by(delivered_by, event.get("delivered_by_identity"))))
    print(_row("✓" if opened_at else "✗", "opened", opened_at,
               _fmt_by(opened_by, event.get("opened_by_identity"))))


def _validate_recipient_project(
    recipient: str, target_project_id: str, channel: str,
):
    """Cross-project pre-flight for ``beacon bus send --to <recipient>``.

    2026-06-09 dogfood で発見された silent-misroute パターン: sender の CLI が
    cwd project に post するが、 recipient は別 project を polling → event
    は書かれるが配信されない (「届いたか?」の唯一の signal が delivered receipt
    の absent = negative signal で気付きにくい)。

    ms-94 / e-2291 (2026-07-06 全面改修): 従来は local bridge 依存の
    ``dm_discover`` (= 送信元マシンで走ってる bridge しか見えない) で
    lookup していたので、 remote / bridge 未起動の recipient が not-found →
    soft-warn → cwd fallback → silent misroute の失敗経路が残っていた。 サーバー
    経由 (``list_sessions`` / ``list_user_sessions``) の lookup に置換し、 remote
    session も見つけられるようにする。 not-found を **hard error に昇格** し
    ("silent misroute 防止")、 fall-back は明示的な opt-out env でのみ許容。

    Resolution policy (new):
      1. target project の全 session を server 側 lookup (``list_sessions``)、
         recipient sid が居れば pass-through。
      2. 居なければ caller の全 project (``list_user_sessions``) を横断 lookup、
         別 project で見つかれば auto-route (loud warning、 audit 可能)。
      3. どこにも居なければ **hard error** (silent misroute を防ぐ、 従来の
         soft-warn は opt-out env でのみ復活)。

    Returns the project_id to actually use. May differ from
    ``target_project_id`` when auto-routing.

    Opt-outs (in this order):
      * ``BEACON_BUS_SKIP_TO_CHECK=1`` — bypass entirely (CI / scripts
        that have already validated routing).
      * ``BEACON_BUS_NO_AUTO_ROUTE=1`` — keep the check, but refuse to
        auto-route on mismatch (emits a hard error so the script breaks
        instead of silently switching project).
      * ``BEACON_BUS_ALLOW_UNKNOWN=1`` — not-found を hard error にしない
        legacy soft-warn 経路に戻す (= 破壊的変更を吸収したい既存 script 用)。
        default では unknown recipient は refuse。
    """
    if channel != "dm" or not recipient:
        return target_project_id
    if os.environ.get("BEACON_BUS_SKIP_TO_CHECK", "") == "1":
        return target_project_id

    # ms-94 / e-2291: server-side lookup で local bridge 依存を切る。 API 失敗
    # (= network hiccup / auth 未設定 等) は defensive fall-through、 発信自体は
    # 妨げない (= 「防御 guard が発信を殺す」 逆転 footgun 防止)。
    client, _config = _get_api_client()

    same_project = False
    other_pid: Optional[str] = None
    lookup_failed = False

    # Step 1: target project 直接 lookup。 since_min は 1 日 (= 1440min) にして
    # bridge 停止直後 の recipient も救う (= e-1402 stale sid 検出とバランス)。
    try:
        target_sessions = client.list_sessions(
            target_project_id, since_minutes=1440,
        )
        for s in target_sessions:
            if s.get("session_id") == recipient:
                same_project = True
                break
    except Exception:
        lookup_failed = True

    if same_project:
        return target_project_id

    # Step 2: caller の全 project 横断 lookup (= /me/sessions endpoint 経由)。
    if not lookup_failed:
        try:
            my_sessions = client.list_user_sessions(since_minutes=1440)
            for s in my_sessions:
                if s.get("session_id") == recipient:
                    other_pid = s.get("project_id") or None
                    break
        except Exception:
            lookup_failed = True

    if other_pid and other_pid != target_project_id:
        if os.environ.get("BEACON_BUS_NO_AUTO_ROUTE", "") == "1":
            print(
                f"Error: recipient session {recipient!r} polls project "
                f"{other_pid!r}, not the target {target_project_id!r}. "
                f"Pass --project {other_pid} explicitly, or unset "
                f"BEACON_BUS_NO_AUTO_ROUTE to let the send auto-route.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(
            f"⚠ recipient session {recipient[:24]}… polls project "
            f"{other_pid!r}, not the target {target_project_id!r}. "
            f"Auto-routing this DM via --project {other_pid}. "
            f"(Set BEACON_BUS_SKIP_TO_CHECK=1 to bypass this check, "
            f"or BEACON_BUS_NO_AUTO_ROUTE=1 to make the mismatch a "
            f"hard error.)",
            file=sys.stderr,
        )
        return other_pid

    # Step 3: どこにも見つからない。 lookup 自体が失敗した場合は API/network 問題
    # を防御的に fall-through (発信は続行、 legacy soft-warn 相当)。 lookup 成功
    # かつ recipient 見つからなかった場合は e-2291 の new behavior として hard
    # error に昇格 (opt-out あり)。
    if lookup_failed:
        print(
            f"⚠ recipient session {recipient[:24]}… の project lookup が "
            f"API 経由で失敗しました。 Send proceeding against project "
            f"{target_project_id!r} as-is; verify the receipt afterwards "
            f"with `beacon bus status <event_id>`.",
            file=sys.stderr,
        )
        return target_project_id

    if os.environ.get("BEACON_BUS_ALLOW_UNKNOWN", "") == "1":
        # Legacy soft-warn: 既存 script との後方互換 opt-out。
        print(
            f"⚠ recipient session {recipient[:24]}… is not visible in any "
            f"of your projects. Send proceeding against project "
            f"{target_project_id!r} as-is; verify the receipt afterwards "
            f"with `beacon bus status <event_id>`. (Set via "
            f"BEACON_BUS_ALLOW_UNKNOWN=1.)",
            file=sys.stderr,
        )
        return target_project_id

    # e-2291 デフォルト新挙動: hard error。 silent misroute (= 誤 project に
    # event が書かれて配信されない) を構造的に防止。
    print(
        f"Error: recipient session {recipient[:24]}… は target project "
        f"{target_project_id!r} にも caller の他 project にも見つかりません。"
        f" silent misroute (= 誤 project に配信されない event を書く) を防ぐ"
        f"ため送信を refuse します。 ms-94 / e-2291 (2026-07-06 新挙動)。\n"
        f"  対処:\n"
        f"    (a) 相手が実在するなら `beacon bus directory` で live 状態と "
        f"project を確認、正しい sid を渡してください。\n"
        f"    (b) 相手が別マシンでオフラインなら user 単位で送る "
        f"`--to-user <user_id>` に切替 (= ms-54 / e-2934、 sid churn 耐性)。\n"
        f"    (c) 意図的に 「見つからなくても送りたい」 場合は "
        f"BEACON_BUS_ALLOW_UNKNOWN=1 で legacy soft-warn 経路に戻せます。",
        file=sys.stderr,
    )
    sys.exit(1)


def cmd_bus_directory():
    """Look up live sessions for DM target selection (ms-54 / e-1134 / e-1151).

    Wraps GET /sessions (cwd project) or GET /me/sessions (cross-project) with
    the directory-query filters. The output is intentionally human-pickable
    (one line per session showing session_id + actor identity + last_active) —
    a sender reads this, picks a session_id, and passes it as the recipient
    for `bus send`. JSON mode is for scripts that want to auto-route (e.g.
    "send to every live agent of user X").

    Default (ms-94 / e-2291, 2026-07-06 reversal): user が member の全 project
    横断 (= /api/me/sessions endpoint 経由)。 「他 project の live session が
    見えない」 という cwd 限定 default の footgun (= 「セッションがいません」
    誤報告の直接原因、 かつての phantom flag --all-projects を silent に
    skip していた病理) を構造的に解消する。

    ``--cwd-only`` (env ``BEACON_DIR_CWD_ONLY``): opt-in で cwd project 限定
    モード。 default が cross-project になった逆パターン、 明示的に cwd 内に
    scope 限定したい (= script / test / 差分 audit 用途) 時のみ指定する。

    ``--project <id>`` (env ``BEACON_BUS_PROJECT_ID``) lets the caller list
    sessions of a specific different project (implies cwd-only semantics with
    the target project). The auth + API URL still come from the current
    project's cloud.json; only the project_id in the API call changes.

    ``--healthy`` (env ``BEACON_DIR_HEALTHY``, ms-54 e-1318): only return
    sessions whose bridge poll loop is actively pumping events into the AI
    inbox right now. Opt-in so existing callers' wire shape is unchanged.
    The JSON output ALWAYS includes the ``poll_health`` block per row (even
    without --healthy) so scripts can decide their own threshold.
    """
    client, config = _get_api_client()

    # ms-94 / e-2291: default reversal — cross-project unless caller opted
    # into cwd 限定 (--cwd-only) or targeted a specific project (--project).
    cwd_only = os.environ.get("BEACON_DIR_CWD_ONLY", "") == "1"
    explicit_project = os.environ.get("BEACON_BUS_PROJECT_ID", "").strip()

    user_id = os.environ.get("BEACON_DIR_USER", "").strip()
    machine = os.environ.get("BEACON_DIR_MACHINE", "").strip()
    agent = os.environ.get("BEACON_DIR_AGENT", "").strip()
    live_only = os.environ.get("BEACON_DIR_LIVE", "") == "1"
    since_minutes = int(os.environ.get("BEACON_DIR_SINCE_MIN", "5") or "5")
    healthy_only = os.environ.get("BEACON_DIR_HEALTHY", "") == "1"

    if cwd_only or explicit_project:
        # 従来経路: cwd project 限定 or 明示 --project 指定先の限定 lookup。
        project_id = _resolve_bus_project_id(config)
        sessions = client.list_sessions(
            project_id,
            user_id=user_id,
            machine=machine,
            agent=agent,
            live_only=live_only,
            since_minutes=since_minutes,
            healthy_only=healthy_only,
        )
    else:
        # 新 default (ms-94 / e-2291): user が member の全 project 横断 lookup。
        # /api/me/sessions endpoint 経由 (= cmd_sessions_list と同じ経路)、 各行
        # に project_id / project_name が付いて返るので picker で識別可能。
        sessions = client.list_user_sessions(
            live_only=live_only,
            since_minutes=since_minutes,
            healthy_only=healthy_only,
            machine=machine,
            agent=agent,
        )
        # user_id filter は list_user_sessions では auth 済 caller の user_id が
        # 暗黙前提なので、 別 user 指定はできない (= 各 user の scope で分離)。
        # 明示 user_id filter が必要な場合は --cwd-only + --user で cwd-scope
        # に絞る fall-back 経路になる。 rare use-case なので stderr で案内。
        if user_id:
            print(
                "Note: --user filter は --cwd-only モード時のみ有効です "
                "(default 経路は auth caller 本人の全 project scope)。"
                " --cwd-only を追加してください。",
                file=sys.stderr,
            )
    if os.environ.get("BEACON_JSON", "") == "1":
        print(json.dumps(sessions, ensure_ascii=False))
        return
    if not sessions:
        print("(no matching sessions)")
        return
    for s in sessions:
        actor = s.get("actor") or {}
        sid = s.get("session_id", "?")
        email = actor.get("email", "")
        machine = actor.get("machine", "")
        agent = actor.get("agent", "")
        last = (s.get("last_active") or "")[:19]
        ident = " / ".join(p for p in (email, machine, agent) if p) or "(anon)"

        # e-1318: surface poll_health in the human picker so users can
        # tell at a glance "this session's bridge is actually polling"
        # vs. "the heartbeat code path ran but the bridge is dead".
        ph = s.get("poll_health") or {}
        healthy = ph.get("healthy")
        age = ph.get("age_seconds")
        shutdown = ph.get("shutdown")
        if shutdown:
            health_tag = "  health=shutdown"
        elif healthy is True:
            age_str = f"{int(age)}s" if isinstance(age, (int, float)) else "?"
            health_tag = f"  health=ok({age_str})"
        elif healthy is False:
            age_str = f"{int(age)}s" if isinstance(age, (int, float)) else "?"
            health_tag = f"  health=stale({age_str})"
        else:
            health_tag = "  health=unknown"

        # ms-94 / e-2291: cross-project default では 各 row に project_name/id
        # が入るので human picker に接頭辞として添える (= 「どの project の
        # session か」 が即分かる、 誤送信防止に直結)。 cwd-only モードでは
        # project_id は暗黙 (= cwd と同じ) なので prefix 省略。
        pid = s.get("project_id", "")
        pname = s.get("project_name", "") or pid
        if pname and not cwd_only and not explicit_project:
            print(f"  [{pname}]  {sid}  {ident}  last_active={last}{health_tag}")
        else:
            print(f"  {sid}  {ident}  last_active={last}{health_tag}")
