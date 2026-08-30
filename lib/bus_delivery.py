"""Shared bus delivery / auto-execute downgrade logic (ms-160 e-5803).

The Claude bus-inbox hook (``bin/beacon-bus-inbox-hook.py``) and the Codex inbox
hook (``scripts/codex-inbox-hook.py``) both receive bus events and must make the
SAME safety decision for ``auto-execute`` events:

  1. An ``auto-execute`` event whose channel is NOT in the project's
     ``bus_auto_execute_channels`` allowlist is downgraded to ``propose-to-ai``
     (a session must not run a Skill it was never opted-in to).
  2. An ``auto-execute`` event on a system-provenance channel
     (operation-trigger / trek-*) whose persisted envelope is NOT a server-minted
     T1-system one is ALSO downgraded — otherwise a project editor could forge a
     ``/beacon-… <attacker-id>`` imperative.
  3. A surviving opted-in ``operation-trigger`` event is rendered as a structured
     "AUTONOMOUS ACTION" block that tells the AI to run
     ``/beacon-operation-execute <op-id>`` without asking first.

Before this module, all of the above lived ONLY in the Claude hook; the Codex
hook rendered every event as a generic DM, so a Codex session neither downgraded
un-allowlisted auto-execute events nor surfaced the operation-trigger imperative.
This module is the ONE place that logic lives; both hooks import it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# Delivery classes (mirror server-side BusEventCreate + bus.mjs).
AUTO_EXECUTE = "auto-execute"
PROPOSE_TO_AI = "propose-to-ai"
NOTIFY_USER_ONLY = "notify-user-only"

OPERATION_TRIGGER_CHANNEL = "operation-trigger"

# Channels whose auto-execute events are routed into a Level 3 imperative block.
# An auto-execute event here is only honored when it also carries a server-minted
# T1-system envelope (see has_system_provenance); otherwise it is downgraded.
SYSTEM_PROVENANCE_CHANNELS = frozenset({
    "operation-trigger",
    "trek-trigger",
    "trek-progress-check",
    "trek-task-review",
    "trek-leader-digest",
})

T1_SYSTEM_TIER = "T1-system"
T1_SYSTEM_ISSUER = "beacon-system"

# Downgrade reason tags (surfaced in the audit frame / inject note).
# e-5803 review (AX-8/AX-9): the allowlist-miss sentinel MUST stay the empty
# string. The Claude hook attaches ``_downgrade_reason`` to an event only when the
# reason is truthy (``if downgrade_reason:``); an allowlist miss historically
# carried NO reason key, so a non-empty value would change that hook's on-wire
# event shape + audit frame (breaking parity with the pre-e-5803 inline logic).
# It is ``_``-prefixed (private) so a caller can't ``import`` it and write
# ``reason == bd._DOWNGRADE_ALLOWLIST_MISS`` — which would alias "unset" and
# misclassify any empty reason. Branch on ``reason == DOWNGRADE_NON_SYSTEM_ENVELOPE``
# and treat the else as allowlist-miss (or use ``if not reason``).
_DOWNGRADE_ALLOWLIST_MISS = ""
DOWNGRADE_NON_SYSTEM_ENVELOPE = "non-system-envelope"

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_-]")


def sanitize_id(raw: object, *, fallback: str = "?", maxlen: int = 64) -> str:
    """Strip an id down to ``[A-Za-z0-9_-]`` and cap its length.

    Payload-derived ids (op_id / trek_id / task_id) are interpolated into the
    imperative command strings; this defends the injected instruction against a
    malformed id (newline / markdown / shell metacharacter) leaking in. Returns
    ``fallback`` when nothing survives so the block never emits an empty command.
    """
    cleaned = _SAFE_ID_RE.sub("", str(raw or ""))[:maxlen]
    return cleaned or fallback


def has_system_provenance(ev: dict) -> bool:
    """True when the event carries a persisted T1-system envelope.

    The server persists ``body.envelope`` on the event after verifying its
    signature, so reading the claimed ``tier`` / ``issuer`` here is sound: a fake
    T1-system claim would have been degraded to T5 (and stripped of auto-execute)
    before persist.
    """
    env = ev.get("envelope")
    if not isinstance(env, dict):
        return False
    return (
        env.get("tier") == T1_SYSTEM_TIER
        and env.get("issuer") == T1_SYSTEM_ISSUER
    )


def read_auto_execute_channels(root: "str | Path") -> list[str]:
    """Read the project's auto-execute allowlist from ``.beacon/project.json``.

    Fail-closed: any read error (missing file, corrupt JSON, wrong type) is
    treated as "no channels armed". The receiver-side downgrade is the safety
    net — an unreadable allowlist must NOT be confused for "allow everything".
    """
    path = Path(root) / ".beacon" / "project.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    raw = data.get("bus_auto_execute_channels")
    if not isinstance(raw, list):
        return []
    return [c for c in raw if isinstance(c, str) and c]


def classify_auto_execute(
    event: dict,
    *,
    allowlist,
    provenance_channels=SYSTEM_PROVENANCE_CHANNELS,
) -> tuple:
    """Resolve one event's effective delivery + downgrade metadata.

    Returns ``(delivery, downgraded_from, downgrade_reason)``:

      * non-auto-execute event → ``(delivery, "", "")`` unchanged.
      * auto-execute, channel not in ``allowlist`` →
        ``("propose-to-ai", "auto-execute", "")`` (allowlist miss, no reason tag).
      * auto-execute, channel is a provenance channel without a T1-system
        envelope → ``("propose-to-ai", "auto-execute", "non-system-envelope")``.
      * auto-execute, opted-in and (if provenance-gated) system-minted → kept:
        ``("auto-execute", "", "")``. The caller routes a kept event by channel
        (operation-trigger → imperative block, trek-* → their blocks).

    This is the exact decision the Claude hook's main loop used inline; both hooks
    now share it so a downgrade rule can't drift between them.
    """
    delivery = event.get("delivery") or PROPOSE_TO_AI
    if delivery != AUTO_EXECUTE:
        return (delivery, "", "")
    channel = str(event.get("channel") or "")
    if channel not in allowlist:
        return (PROPOSE_TO_AI, AUTO_EXECUTE, _DOWNGRADE_ALLOWLIST_MISS)
    if channel in provenance_channels and not has_system_provenance(event):
        return (PROPOSE_TO_AI, AUTO_EXECUTE, DOWNGRADE_NON_SYSTEM_ENVELOPE)
    return (AUTO_EXECUTE, "", "")


def format_operation_trigger_imperative(events: list) -> str:
    """Render the "AUTONOMOUS ACTION" inject for opted-in operation-trigger events.

    Scope boundary (e-5803 review / Maint-3): this module currently shares ONLY
    the operation-trigger imperative. The trek-channel Level-3 imperatives
    (trek-trigger / trek-progress-check / trek-task-review / trek-leader-digest)
    still live as Claude-hook-only formatters in bin/beacon-bus-inbox-hook.py and
    are NOT rendered by the Codex inbox hook. That is intentional for now (e-5803's
    scope was operation-trigger + the downgrade gate), not an oversight — a Codex
    session gets the generic event list for trek DMs, not the forced-invoke block.
    Extending trek parity to Codex is deliberately deferred to a follow-up so this
    module isn't a half-migrated home that invites duplicate trek formatters.


    Each event is an opted-in auto-execute event on the ``operation-trigger``
    channel (already passed ``classify_auto_execute`` as kept). The block tells
    the AI to run ``/beacon-operation-execute`` autonomously and lifts the
    op_id / spec_doc_id / trigger_name out of the payload. Returns "" for an
    empty list so callers can just check truthiness.
    """
    if not events:
        return ""
    lines: list[str] = []
    lines.append("## AUTONOMOUS ACTION — operation autonomy active")
    lines.append("")
    lines.append(
        "以下の operation-trigger event は `bus_auto_execute_channels` "
        "allowlist に登録済 (= 人間 opt-in 済) のため、"
        "**確認を取らず autonomous に実行する**。"
    )
    lines.append("Run this autonomously without asking the user first.")
    lines.append("")
    for ev in events:
        payload = ev.get("payload") or {}
        op_id = sanitize_id(payload.get("op_id"))
        spec_doc_id = sanitize_id(payload.get("spec_doc_id"), fallback="")
        trigger_name = payload.get("trigger_name", "")
        eid = ev.get("event_id", "?")
        lines.append(f"- event_id: {eid}")
        lines.append(f"  - op_id: {op_id}")
        if spec_doc_id:
            lines.append(f"  - spec_doc_id: {spec_doc_id}")
        if trigger_name:
            lines.append(f"  - trigger_name: {trigger_name}")
        cmd = f"/beacon-operation-execute {op_id}"
        lines.append(f"  - launch: `{cmd}`  (no confirmation prompt)")
    lines.append("")
    lines.append(
        "budget が枯渇していたら Skill 側 (Step 4.5) が自動で停止 → incident "
        "open → note 残しに降格する。Skill の責務なのでこの inject 側で "
        "事前判定はしない。"
    )
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "AUTO_EXECUTE",
    "PROPOSE_TO_AI",
    "NOTIFY_USER_ONLY",
    "OPERATION_TRIGGER_CHANNEL",
    "SYSTEM_PROVENANCE_CHANNELS",
    "T1_SYSTEM_TIER",
    "T1_SYSTEM_ISSUER",
    "DOWNGRADE_NON_SYSTEM_ENVELOPE",
    "sanitize_id",
    "has_system_provenance",
    "read_auto_execute_channels",
    "classify_auto_execute",
    "format_operation_trigger_imperative",
]
